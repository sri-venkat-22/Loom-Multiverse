"""WP-4.7 — the whole product, from a sentence. FR-PIPE-01, R1 acceptance.

WP-4.5 proved a cheap model can build a repo from a Design somebody wrote by hand. This proves
Loom can write that Design itself: an idea string goes in, four artifacts and a graded
repository come out, unattended, for under $2.

    make pipeline
    # or: uv run pytest -m live tests/e2e/test_pipeline_urlshortener.py -q -s

Real API calls and real money, so it is `@pytest.mark.live` and skipped unless the `-m`
expression names that tier. It prints a full report on both paths, for the same reason WP-4.5's
does: a run that costs two dollars and tells you only "assert False" has wasted most of what you
paid for. Here the report matters more — when this fails, the interesting question is *which
phase* produced the artifact that made the rest impossible, and that is only visible if the
artifacts are printed.

Knobs, all environment variables:

    LOOM_E2E_MODEL       the phases and the builder   (default: the cheap tier)
    LOOM_E2E_JUDGE_MODEL the rubric judge             (default: the same cheap model)
    LOOM_E2E_BUDGET      the run ceiling              (default: 2.00 — the plan's number)
    LOOM_E2E_MAX_TURNS   the turn cap per phase       (default: 40)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from loom.agent.providers import LiteLLMProvider
from loom.agent.tools.ask_user import AskUser
from loom.cache import PhaseCache
from loom.config import Config
from loom.ledger import Ledger
from loom.pipeline import PHASES, PipelineResult, run_pipeline
from loom.session import Session
from loom.workspace import Workspace

# The milestone module owns the credential plumbing and the skip reason; there is one copy of
# both, and it is the one WP-4.5 already had to get right.
from tests.e2e.test_build_urlshortener import JUDGE_MODEL, MODEL, needs_key

IDEA = "a URL shortener with click analytics"

BUDGET = float(os.environ.get("LOOM_E2E_BUDGET", "2.00"))
MAX_TURNS = int(os.environ.get("LOOM_E2E_MAX_TURNS", "40"))

#: The threshold the acceptance criterion names. The Design phase writes its own, and a design
#: that lowers its own bar is a failure of this test, not a pass — hence the assertion below.
REQUIRED_THRESHOLD = 0.85


@pytest.mark.live
@needs_key
async def test_an_idea_string_becomes_four_artifacts_and_a_graded_repo(tmp_path: Path) -> None:
    """R1 acceptance: `loom run "<idea>"` end to end, unattended, under the budget."""
    Workspace.create(tmp_path)
    ledger = Ledger(tmp_path / ".loom" / "ledger.db")
    session = Session(tmp_path, "e2e")
    events: list[tuple[str, dict[str, Any]]] = []

    def record(kind: str, **fields: Any) -> None:
        events.append((kind, fields))
        session.log_event(kind, **fields)

    config = Config(
        model=MODEL,
        judge_model=JUDGE_MODEL,
        max_turns=MAX_TURNS,
        max_usd=BUDGET,
        budget_usd=BUDGET,
    )

    def provider_for(phase: str) -> LiteLLMProvider:
        return LiteLLMProvider(MODEL, ledger=ledger, phase=phase, run_id="e2e", on_event=record)

    try:
        result = await run_pipeline(
            IDEA,
            root=tmp_path,
            provider_factory=provider_for,
            config=config,
            # No ledger of its own: `grade()` records judge spend under phase "judge" already.
            judge_provider=LiteLLMProvider(JUDGE_MODEL),
            session=session,
            # A fresh cache per run: a hit would make this test assert on last week's artifacts.
            cache=PhaseCache(tmp_path, enabled=False),
            # Unattended. A question with nobody to answer it would hang the run.
            ask_user_fn=AskUser(yes=True, on_event=record),
            budget_usd=BUDGET,
        )
    except Exception:
        print(report(None, tmp_path, ledger, events))
        raise

    print(report(result, tmp_path, ledger, events))

    assert result.status == "passed", (
        f"exited {result.status!r} after {result.phases_run} and ${result.usd_spent:.4f}. "
        f"{result.message}\nSee the report above and {tmp_path}."
    )
    assert result.phases_run == list(PHASES)
    for phase in PHASES:
        assert Path(result.artifacts[phase]).is_file(), f"{phase} left no artifact"

    # The Design phase writes its own rubric, so a run could "pass" by writing an easy one.
    # The acceptance number is ours, not the model's.
    assert result.design is not None
    assert result.design.rubric.threshold >= REQUIRED_THRESHOLD, (
        "the design lowered its own bar to "
        f"{result.design.rubric.threshold} — that is a failed run, not a passed one"
    )
    assert result.build is not None and result.build.score is not None
    assert result.build.score.total >= REQUIRED_THRESHOLD
    assert result.usd_spent < BUDGET, "cleared the rubric, but not inside the budget"

    # FR-DES-03 held in production, not only against FakeLLM.
    criteria = result.design.rubric.criteria
    assert len(criteria) >= 2
    assert sum(c.weight for c in criteria) == pytest.approx(1.0, abs=0.02)
    assert any(c.kind == "shell" for c in criteria)

    # The manifest is part of the deal: a rubric cleared by a repo missing files the design
    # asked for is a rubric with the wrong weights.
    for spec in result.design.file_manifest:
        assert (tmp_path / spec.path).is_file(), f"the design asked for {spec.path}"

    assert result.build.commit is not None, "a passed build should be squashed into one commit"
    assert not Workspace.open(tmp_path).is_dirty()


def report(
    result: PipelineResult | None,
    workspace: Path,
    ledger: Ledger,
    events: list[tuple[str, dict[str, Any]]],
) -> str:
    """Everything two dollars should buy you, whether or not the assertions pass.

    The artifacts are printed in full. When this test fails the question is almost always
    "which phase wrote the thing that made the rest impossible", and nothing else answers it.
    """
    lines = [
        "",
        "─" * 78,
        f"  WP-4.7 — {IDEA!r} on {MODEL}",
        "─" * 78,
        f"  status     {result.status if result else 'DIED — see the traceback below'}",
        f"  phases     {', '.join(result.phases_run) if result else _phases(events)}",
        f"  spend      ${(result.usd_spent if result else ledger.total()):.4f} of ${BUDGET:.2f}",
        f"  workspace  {workspace}",
    ]

    if result is not None and result.build is not None:
        build = result.build
        lines += [
            f"  rubric     {_total(build)} (threshold "
            f"{result.design.rubric.threshold if result.design else '—'})",
            f"  build      {build.status}, {build.turns_used} turns, {build.rounds} rounds",
        ]
        for criterion in build.score.criteria if build.score else []:
            mark = "pass" if criterion.score >= 1.0 else "FAIL"
            lines.append(
                f"    [{mark}] {criterion.name:<14} {criterion.score:.2f} "
                f"× {criterion.weight:.2f}  {_one_line(criterion.detail)}"
            )

    lines += ["", "  spend by phase"]
    for phase, usd in ledger.by_phase().items():
        lines.append(f"    {phase:<12} ${usd:.4f}")

    lines += ["", "  artifacts"]
    for phase, path in (result.artifacts if result else {}).items():
        lines.append(f"    {phase:<12} {path}")

    lines += ["", "  files produced"]
    for path in sorted(_repo_files(workspace)):
        lines.append(f"    {path}")

    retries = sum(1 for kind, _ in events if kind == "retry")
    asked = sum(1 for kind, _ in events if kind == "ask_user")
    tools = [f.get("name") for kind, f in events if kind == "tool_call"]
    lines += [
        "",
        f"  tool calls   {len(tools)}: "
        + ", ".join(f"{n}×{tools.count(n)}" for n in sorted(set(tools))),
        f"  repairs      {retries}  (a structured-output repair costs a call, FR-PIPE-06)",
        f"  questions    {asked}  (answered with the declared default; --yes)",
        "",
        "  artifacts, in full",
        "─" * 78,
    ]
    for phase, path in (result.artifacts if result else {}).items():
        lines += [f"  ── {phase} ──", _indent(Path(path).read_text(encoding="utf-8"))]
    lines += ["─" * 78, f"  event log    {workspace / '.loom' / 'runs' / 'e2e' / 'events.jsonl'}"]
    return "\n".join(lines)


def _phases(events: list[tuple[str, dict[str, Any]]]) -> str:
    done = [f.get("phase", "?") for kind, f in events if kind == "phase_finished"]
    return ", ".join(done) or "none finished"


def _total(build: Any) -> str:
    return "—" if build.score is None else f"{build.score.total:.3f}"


def _one_line(detail: str, width: int = 58) -> str:
    flat = " ".join(detail.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())


def _repo_files(workspace: Path) -> list[str]:
    return [
        str(p.relative_to(workspace))
        for p in workspace.rglob("*")
        if p.is_file()
        and not any(
            part in {".git", ".loom", ".venv", "__pycache__", ".pytest_cache"}
            for part in p.relative_to(workspace).parts
        )
    ]


def test_the_harness_is_wired_before_anyone_pays_for_it(tmp_path: Path) -> None:
    """Not live. The parts that can silently make this test unpassable, checked for free.

    WP-4.5 learned this the expensive way: a harness that only runs when it costs money is a
    harness whose bugs are only ever found while paying.
    """
    assert REQUIRED_THRESHOLD == 0.85  # the R1 acceptance number, SRS §9.1
    assert BUDGET <= 2.00, "the plan's ceiling for WP-4.7 is $2"
    assert set(PHASES) == {"validate", "plan", "design", "build"}

    # The report must survive a run that died before producing anything.
    text = report(None, tmp_path, Ledger(tmp_path / "ledger.db"), [])
    assert "DIED" in text and "none finished" in text
    assert json.dumps(text)  # no unprintable surprises
