"""Re-run one phase against a finished run's artifacts. WP-3.7 — FR-SESS-05.

The most-repeated action in this project is: change one paragraph of a prompt, run that phase
again, and look at what moved. Without this, that costs a full pipeline — four phases, several
minutes and real money — to see the effect of an edit to the fourth one. With it, it costs one
provider call and a diff.

Three deliberate choices:

* **The cache is bypassed.** The whole point is to see a *new* answer, and an unchanged prompt
  would otherwise serve the old one from `.loom/cache/` and make zero calls.
* **Nothing is overwritten.** The replay writes `design.replay-1.json` beside `design.json`.
  Comparing two prompts is worthless if running the second one destroys the first one's output.
* **Upstream artifacts are re-validated on the way in**, by `load_artifact` — a hand-edited
  `prd.json` is exactly the input someone replays design against (FR-ART-03).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from loom.agent.tools.fs import atomic_write
from loom.config import Config
from loom.contracts import Provider, Validation
from loom.gates import diff as diff_artifacts
from loom.phases.base import PhaseOutcome, artifact_path
from loom.pipeline import ARTIFACT_MODELS, inputs_for, make_phase, read_notes
from loom.session import Session

_SLOT = re.compile(r"\.replay-(\d+)\.json$")


class ReplayResult(BaseModel):
    """What the CLI prints and what a test asserts on."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    phase: str
    artifact: Any
    path: Path
    previous_path: Path
    diff: str
    usd_spent: float = 0.0
    prompt_path: Path | None = None


def next_slot(root: Path, run_id: str, phase: str) -> Path:
    """`<phase>.replay-N.json`, N being one past the highest already there."""
    base = artifact_path(root, run_id, phase)
    taken = [
        int(m.group(1))
        for p in base.parent.glob(f"{base.stem}.replay-*.json")
        if (m := _SLOT.search(p.name))
    ]
    return base.with_name(f"{base.stem}.replay-{max(taken, default=0) + 1}.json")


def latest_artifact(root: Path, run_id: str, phase: str) -> Path:
    """The most recent slot for a phase — the previous replay if there is one, else the run's
    own artifact. Replaying twice in a row should diff against the last thing you looked at."""
    base = artifact_path(root, run_id, phase)
    slots = sorted(
        (p for p in base.parent.glob(f"{base.stem}.replay-*.json") if _SLOT.search(p.name)),
        key=lambda p: int(_SLOT.search(p.name).group(1)),  # type: ignore[union-attr]
    )
    return slots[-1] if slots else base


async def replay(
    *,
    root: Path,
    run_id: str,
    phase: str,
    provider: Provider,
    config: Config,
    prompt_path: Path | None = None,
    session: Session | None = None,
    ask_user_fn: Callable[..., Any] | None = None,
    fetcher: Any = None,
) -> ReplayResult:
    """Run `phase` once more against run `run_id`'s upstream artifacts."""
    root = Path(root)
    if phase not in ARTIFACT_MODELS:
        raise ValueError(
            f"cannot replay {phase!r}: only {', '.join(ARTIFACT_MODELS)} produce a replayable "
            "artifact. A build is replayed by resetting the workspace and running it again."
        )

    previous_path = latest_artifact(root, run_id, phase)
    if not previous_path.is_file():
        raise FileNotFoundError(
            f"run {run_id} has no {phase} artifact at {previous_path}; there is nothing to "
            "replay against."
        )
    previous = ARTIFACT_MODELS[phase].model_validate_json(previous_path.read_text(encoding="utf-8"))

    session = session or Session(root, run_id)
    instance = make_phase(phase, fetcher=fetcher)
    if prompt_path is not None:
        instance.prompt_path = Path(prompt_path)

    idea = _idea_for(root, run_id)
    outcome: PhaseOutcome = await instance.execute(
        provider=provider,
        inputs=inputs_for(
            phase, idea=idea, notes=read_notes(root), produced={}, root=root, run_id=run_id
        ),
        root=root,
        run_id=run_id,
        max_turns=config.max_turns,
        max_usd=config.max_usd,
        cache=None,  # FR-SESS-05 — one real call, not a cache hit
        on_event=session.log_event,
        ask_user_fn=ask_user_fn,
        persist=False,  # the run's own artifact is not the replay's to overwrite
    )

    path = next_slot(root, run_id, phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, outcome.artifact.model_dump_json(indent=2) + "\n")
    session.log_event("artifact_written", phase=phase, path=str(path), replay=True)

    return ReplayResult(
        run_id=run_id,
        phase=phase,
        artifact=outcome.artifact,
        path=path,
        previous_path=previous_path,
        diff=diff_artifacts(previous, outcome.artifact, phase=phase),
        usd_spent=outcome.usd_spent,
        prompt_path=instance.prompt_path,
    )


def _idea_for(root: Path, run_id: str) -> str:
    """The idea a run started from, out of its own event log.

    Only `validate` needs it, and only a replay of `validate` would fail without it — but
    reading it here means a replay never has to be told something the run already knows.
    """
    for event in Session(root, run_id).read_events():
        if event.get("kind") == "run_started" and event.get("idea"):
            return str(event["idea"])
    validation = artifact_path(root, run_id, "validate")
    if validation.is_file():
        return str(Validation.model_validate_json(validation.read_text(encoding="utf-8")).idea)
    return ""
