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

import json
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
from loom.session import Session, runs_dir
from loom.workspace import Workspace

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


# --------------------------------------------------------------------------- /rewind (FR-SESS-08)


class RewindResult(BaseModel):
    """What `/rewind` reports after restoring the workspace and the build history."""

    run_id: str
    target: str
    turn: int
    sha: str
    messages_kept: int
    transcript_path: Path | None = None


def _turn_number(label: str) -> int:
    """The turn a snapshot label restores to. `scaffold` is turn 0 (before the first turn); a
    non-turn label is -1 so it is filtered out of the rewind targets."""
    if label == "scaffold":
        return 0
    m = re.fullmatch(r"turn-(\d+)", label)
    return int(m.group(1)) if m else -1


def rewind_targets(root: Path, run_id: str) -> list[str]:
    """The snapshots `/rewind` can restore to, earliest first: `scaffold`, then `turn-1…N`. Only
    the build phase takes per-turn snapshots, so a run that never built has none. `run_id` is
    unused today — snapshot refs are per-repo, not per-run — but kept so a future per-run scoping
    is a signature-compatible change."""
    labels = [lbl for lbl in Workspace(Path(root)).snapshots() if _turn_number(lbl) >= 0]
    return sorted(labels, key=_turn_number)


def truncate_transcript(messages: list[dict[str, Any]], turn: int) -> list[dict[str, Any]]:
    """The build transcript as it stood at the end of `turn` (1-based): everything up to, but not
    including, the (turn+1)-th assistant message. Turn 0 keeps only the system+task priming. One
    assistant message per turn is the agent loop's invariant (`agent/loop.py`), so counting them
    is how a flat message list is cut at a turn boundary."""
    if turn < 0:
        return list(messages)
    assistant_seen = 0
    for i, message in enumerate(messages):
        if message.get("role") == "assistant":
            assistant_seen += 1
            if assistant_seen == turn + 1:
                return messages[:i]
    return list(messages)


def rewind(root: Path, run_id: str, target: str) -> RewindResult:
    """FR-SESS-08 — restore the workspace tree and the build message history to `target`, an
    earlier turn snapshot. Destructive by design: the point is to throw away what came after. The
    turn refs stay reachable after a `reset --hard`, so a rewind is itself revertible."""
    root = Path(root)
    turn = _turn_number(target)
    if turn < 0:
        raise ValueError(f"{target!r} is not a turn snapshot; rewind targets are scaffold, turn-N")

    # reset_to spares `.loom/`, so the transcript we are about to trim survives the tree reset.
    sha = Workspace(root).reset_to(target)

    kept = 0
    trimmed_path: Path | None = None
    path = runs_dir(root) / run_id / "transcript.json" if run_id else None
    if path is not None and path.is_file():
        try:
            messages = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            messages = None
        if isinstance(messages, list):
            trimmed = truncate_transcript(messages, turn)
            atomic_write(path, json.dumps(trimmed) + "\n")
            kept, trimmed_path = len(trimmed), path

    # ponytail: no `rewound` event. It is a user action on a paused run, not something the run
    # did, and EVENT_KINDS is closed (a new kind is also an SRS §6 edit). Add one if `loom
    # status` ever needs to show that a rewind happened.
    return RewindResult(
        run_id=run_id,
        target=target,
        turn=turn,
        sha=sha,
        messages_kept=kept,
        transcript_path=trimmed_path,
    )
