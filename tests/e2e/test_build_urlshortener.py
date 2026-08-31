"""WP-4.5 — the milestone. Everything before it is scaffolding; everything after is polish.

The one fact this whole project is organised around: **can a cheap model, driven by our loop,
produce a repo that clears a rubric?** No validate, no plan, no design phase, no pipeline — a
hand-written `Design` goes in, a graded repository comes out.

    make milestone            # or: uv run pytest -m live tests/e2e -q -s

Real API calls and real money, so it is `@pytest.mark.live` and skipped unless the `-m`
expression names that tier. It prints a full report whether it passes or fails, because a run
that costs a dollar and tells you only "assert False" has wasted most of what you paid for.

Knobs, all environment variables:

    LOOM_E2E_MODEL       the builder      (default: the cheap tier from config)
    LOOM_E2E_JUDGE_MODEL the judge        (default: the same cheap model, per the cost rules)
    LOOM_E2E_MAX_USD     the ceiling      (default: 1.00 — the milestone's own budget)
    LOOM_E2E_MAX_TURNS   the turn cap     (default: 40)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from loom.agent.providers import KEY_FOR_PREFIX, LiteLLMProvider, key_variable_for
from loom.agent.tools.ask_user import AskUser
from loom.config import MODEL_TIERS, apply_credentials
from loom.contracts import Design
from loom.ledger import Ledger
from loom.phases.build import BuildResult, run_build
from loom.rubric import Score
from loom.workspace import Workspace

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "urlshortener" / "design.json"

MODEL = os.environ.get("LOOM_E2E_MODEL", MODEL_TIERS["cheap"])
JUDGE_MODEL = os.environ.get("LOOM_E2E_JUDGE_MODEL", MODEL_TIERS["cheap"])
MAX_USD = float(os.environ.get("LOOM_E2E_MAX_USD", "1.00"))
MAX_TURNS = int(os.environ.get("LOOM_E2E_MAX_TURNS", "40"))


def credential_problem() -> str | None:
    """A reason to skip, or None. Never raises.

    Evaluated at import, and this module is imported by every `pytest` run — so a
    wrongly-permissioned credentials file must not be able to break collection for the whole
    suite. It becomes a skip reason here and stays loud inside the live test itself.
    """
    try:
        apply_credentials()  # FR-CFG-06 — a key in ~/.loom/credentials.json works here too
    except (PermissionError, ValueError) as exc:
        return str(exc)
    required = key_variable_for(MODEL)
    if required and not os.environ.get(required):
        return f"{required} is not set — export it, or put it in ~/.loom/credentials.json"
    return None


SKIP_REASON = credential_problem()
needs_key = pytest.mark.skipif(SKIP_REASON is not None, reason=SKIP_REASON or "")


@pytest.mark.live
@needs_key
async def test_a_cheap_model_builds_a_url_shortener_that_clears_its_own_rubric(
    tmp_path: Path,
) -> None:
    """The milestone: `run_build()` on the fixture Design clears 0.85, for under a dollar."""
    design = Design.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    Workspace.create(tmp_path)
    ledger = Ledger(tmp_path / ".loom" / "ledger.db")
    events: list[tuple[str, dict[str, Any]]] = []

    try:
        result = await run_build(
            design,
            workspace=tmp_path,
            provider=LiteLLMProvider(
                MODEL,
                ledger=ledger,
                phase="build",
                run_id="e2e",
                on_event=lambda kind, **f: events.append((kind, f)),
            ),
            # Pinned cheap and ledgered apart from the build, per the cost rules. No ledger of its
            # own: `grade()` records judge spend itself, two ledgers would double-count.
            judge_provider=LiteLLMProvider(JUDGE_MODEL),
            judge_model=JUDGE_MODEL,
            ledger=ledger,
            run_id="e2e",
            max_turns=MAX_TURNS,
            max_usd=MAX_USD,
            # Unattended: a question with nobody to answer it would hang the milestone.
            ask_user_fn=AskUser(yes=True),
            on_event=lambda kind, **f: events.append((kind, f)),
        )
    except Exception:
        # A provider that dies on turn 30 has still spent turn 29's money. Print what was
        # bought before re-raising, or a failed milestone costs a dollar and yields a traceback.
        print(report(None, tmp_path, ledger, events))
        raise

    print(report(result, tmp_path, ledger, events))

    assert result.status == "passed", (
        f"exited {result.status!r} after {result.turns_used} turns and "
        f"${result.usd_spent:.4f}. See the report above and {tmp_path}."
    )
    assert result.score is not None
    assert result.score.total >= design.rubric.threshold
    assert result.usd_spent < MAX_USD, "cleared the rubric, but not inside the budget"

    # The rubric could in principle be cleared by a repo that is missing files the design asked
    # for, if the weights allowed it. The manifest is part of the deal.
    for spec in design.file_manifest:
        assert (tmp_path / spec.path).is_file(), f"the design asked for {spec.path}"

    # The anti-gaming criterion specifically: an acceptance check the model did not write.
    behaviour = next(c for c in result.score.criteria if c.name == "behaviour")
    assert behaviour.score == 1.0, f"the independent acceptance check failed: {behaviour.detail}"

    assert result.commit is not None, "a passed phase should be squashed into one commit"
    assert not Workspace.open(tmp_path).is_dirty()


def report(
    result: BuildResult | None,
    workspace: Path,
    ledger: Ledger,
    events: list[tuple[str, dict[str, Any]]],
) -> str:
    """Everything a dollar should buy you, whether or not the assertions pass."""
    lines = [
        "",
        "─" * 78,
        f"  WP-4.5 milestone — {MODEL}",
        "─" * 78,
        f"  status     {result.status if result else 'DIED — see the traceback below'}",
        f"  score      {_total(result.score if result else None)}",
        f"  turns      {result.turns_used if result else _turns(events)} of {MAX_TURNS}",
        f"  rounds     {result.rounds if result else sum(1 for k, _ in events if k == 'graded')}",
        f"  spend      ${result.usd_spent if result else ledger.total():.4f} of ${MAX_USD:.2f}",
        f"  workspace  {workspace}",
        "",
        "  criteria",
    ]
    for criterion in result.score.criteria if result and result.score else []:
        mark = "pass" if criterion.score >= 1.0 else "FAIL"
        lines.append(
            f"    [{mark}] {criterion.name:<12} {criterion.score:.2f} "
            f"× {criterion.weight:.2f}  {_one_line(criterion.detail)}"
        )

    lines += ["", "  spend by phase"]
    for phase, usd in ledger.by_phase().items():
        lines.append(f"    {phase:<12} ${usd:.4f}")

    lines += ["", "  files produced"]
    for path in sorted(_repo_files(workspace)):
        lines.append(f"    {path}")

    tools = [f.get("name") for kind, f in events if kind == "tool_call"]
    lines += [
        "",
        f"  tool calls   {len(tools)}: "
        + ", ".join(f"{n}×{tools.count(n)}" for n in sorted(set(tools))),
        f"  retries      {sum(1 for kind, _ in events if kind == 'retry')}",
        "─" * 78,
    ]
    _write_trace(workspace, events)
    lines.append(f"  event trace  {workspace / '.loom' / 'e2e-events.jsonl'}")
    return "\n".join(lines)


def _turns(events: list[tuple[str, dict[str, Any]]]) -> int:
    return max((int(f.get("turn", 0)) for k, f in events if k == "turn"), default=0)


def _total(score: Score | None) -> str:
    return "—" if score is None else f"{score.total:.3f}"


def _one_line(detail: str, width: int = 60) -> str:
    flat = " ".join(detail.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


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


def _write_trace(workspace: Path, events: list[tuple[str, dict[str, Any]]]) -> None:
    path = workspace / ".loom" / "e2e-events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for kind, fields in events:
            fh.write(json.dumps({"kind": kind, **fields}, default=str) + "\n")


# --------------------------------------------------------------------------- harness guards


def test_the_milestone_harness_is_wired_before_anyone_pays_for_it() -> None:
    """Not live. Catches the ways this file can be broken such that it could never pass —
    which is otherwise discovered only by someone with a key and a spare dollar."""
    design = Design.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    assert design.rubric.threshold >= 0.85
    assert MAX_USD > 0 and MAX_TURNS > 0
    assert "behaviour" in {c.name for c in design.rubric.criteria}, (
        "the assertion on the independent acceptance criterion names a criterion that is gone"
    )
    assert MODEL.split("/")[0] in KEY_FOR_PREFIX, (
        f"no idea which API key {MODEL!r} needs; add its prefix to providers.KEY_FOR_PREFIX"
    )
    # The provider is constructible without a network or a key; only `complete()` needs those.
    from loom.contracts import Provider

    assert isinstance(LiteLLMProvider(MODEL), Provider)
