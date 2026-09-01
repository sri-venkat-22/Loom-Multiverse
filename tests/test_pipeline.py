"""WP-3.6 — the pipeline. FR-PIPE-01/02/03/04, FR-VAL-04, FR-GATE-02/04, FR-HEADLESS-01/04.

Most of this file runs `validate → design` and stops, because those three phases need nothing
but a directory. The four-phase tests need a real git repository and are marked `slow`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from loom.cache import PhaseCache
from loom.config import Config
from loom.contracts import Response, ToolCall, Validation
from loom.gates import Decision
from loom.pipeline import (
    PHASES,
    BudgetExceeded,
    BudgetGuard,
    RunBudget,
    UnattendedWithoutBudget,
    phase_range,
    run_pipeline,
)
from loom.session import Session
from loom.testing.fake_llm import FakeLLM
from loom.workspace import Workspace

VALIDATION = {
    "idea": "a url shortener with click analytics",
    "verdict": "go",
    "reasoning": "small and buildable",
    "competitors": ["Bitly"],
    "risks": ["Bitly bundles analytics"],
    "sources": ["https://bitly.com/pricing"],
}

PRD_OUT = {
    "product_name": "Snip",
    "problem": "Marketers cannot tell which link was clicked.",
    "users": ["newsletter writers"],
    "v0_features": ["shorten a URL", "resolve a code"],
    "non_goals": ["accounts"],
    "success_metrics": ["a second link within a week"],
}


def shell(name: str, weight: float, command: str) -> dict[str, Any]:
    return {"name": name, "kind": "shell", "weight": weight, "command": command}


DESIGN_OUT: dict[str, Any] = {
    "summary": "a slugify library with tests",
    "stack": ["Python"],
    "scaffold_command": None,
    "file_manifest": [{"path": "slugify.py", "purpose": "the slugify function"}],
    "rubric": {
        "threshold": 0.85,
        "hard_fail": [],
        "criteria": [
            shell("tests", 0.5, "python -m pytest -q x"),
            shell("exists", 0.5, "test -f slugify.py"),
        ],
    },
}


def say(payload: dict[str, Any]) -> Response:
    return Response(text=json.dumps(payload), usd_cost=0.01)


def write(path: str, content: str, cid: str = "c1") -> Response:
    return Response(
        tool_calls=[
            ToolCall(id=cid, name="write_file", arguments={"path": path, "content": content})
        ],
        usd_cost=0.01,
    )


SHAPE_A = [say(VALIDATION), say(PRD_OUT), say(DESIGN_OUT)]


def config(**over: Any) -> Config:
    return Config(**{"budget_usd": 5.0, "max_usd": 1.0, "max_turns": 4, **over})


async def pipeline(
    root: Path,
    script: list[Response] | None = None,
    *,
    stop: str = "design",
    fake: FakeLLM | None = None,
    **kw: Any,
) -> Any:
    fake = fake or FakeLLM(list(script if script is not None else SHAPE_A))
    kw.setdefault("cache", PhaseCache(root, enabled=False))
    return await run_pipeline(
        "a url shortener with click analytics",
        root=root,
        provider_factory=lambda phase: fake,
        config=kw.pop("config", None) or config(),
        stop=stop,
        fetcher=lambda url: (url, "<html><body>a page</body></html>"),
        **kw,
    )


# --------------------------------------------------------------------------- FR-PIPE-01


async def test_the_phases_run_in_order_and_each_leaves_an_artifact(tmp_path: Path) -> None:
    result = await pipeline(tmp_path)
    assert result.status == "passed"
    assert result.phases_run == ["validate", "plan", "design"]
    assert set(result.artifacts) == {"validate", "plan", "design"}
    for phase, name in (
        ("validate", "validation.json"),
        ("plan", "prd.json"),
        ("design", "design.json"),
    ):
        assert Path(result.artifacts[phase]).name == name
        assert Path(result.artifacts[phase]).is_file()


async def test_every_phase_leaves_an_approval_event(tmp_path: Path) -> None:
    """FR-PIPE-01 and FR-GATE-04 — a headless run's history is the same as a watched one's."""
    result = await pipeline(tmp_path)
    events = Session(tmp_path, result.run_id).read_events()
    approvals = [e for e in events if e["kind"] == "gate_decision"]
    assert [e["phase"] for e in approvals] == ["validate", "plan", "design"]
    assert all(e["action"] == "approve" for e in approvals)


async def test_the_run_is_bracketed_by_its_own_events(tmp_path: Path) -> None:
    result = await pipeline(tmp_path)
    kinds = [e["kind"] for e in Session(tmp_path, result.run_id).read_events()]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"


# --------------------------------------------------------------------------- FR-PIPE-02


async def test_a_phase_sees_upstream_artifacts_and_no_upstream_transcript(
    tmp_path: Path,
) -> None:
    """The design phase's messages must not contain the validate phase's reasoning about
    Bitly's pricing page — only what the PRD carried forward."""
    fake = FakeLLM(list(SHAPE_A))
    await pipeline(tmp_path, fake=fake)
    text = json.dumps(fake.calls[2])
    assert "shorten a URL" in text  # the PRD reached it
    assert "a page" not in text  # the fetched page did not
    assert "https://bitly.com/pricing" not in text  # nor the validate transcript's sources


async def test_each_phase_starts_from_a_fresh_transcript(tmp_path: Path) -> None:
    fake = FakeLLM(list(SHAPE_A))
    await pipeline(tmp_path, fake=fake)
    for call in fake.calls:
        assert [m["role"] for m in call] == ["system", "user"]


# --------------------------------------------------------------------------- FR-PIPE-03


async def test_the_run_ceiling_aborts_into_a_resumable_state(tmp_path: Path) -> None:
    result = await pipeline(tmp_path, config=config(budget_usd=0.01))
    assert result.status == "budget_exhausted"
    assert result.phases_run == ["validate"]
    # The artifact the run did finish is on disk, so `loom resume` has somewhere to start.
    assert Path(result.artifacts["validate"]).is_file()
    assert "0.02" in result.message or "budget" in result.message


async def test_the_ceiling_is_checked_before_the_call_not_after(tmp_path: Path) -> None:
    """FR-AGENT-02's run-level twin: an abort must not be charged for a completion it has
    already decided to throw away."""
    fake = FakeLLM(list(SHAPE_A))
    result = await pipeline(tmp_path, fake=fake, config=config(budget_usd=0.01))
    assert fake.call_count == 1
    assert result.usd_spent == pytest.approx(0.01)


async def test_a_budget_of_zero_never_calls_a_provider(tmp_path: Path) -> None:
    fake = FakeLLM(list(SHAPE_A))
    result = await pipeline(tmp_path, fake=fake, budget_usd=0.0)
    assert fake.call_count == 0
    assert result.status == "budget_exhausted"
    assert result.phases_run == []


async def test_the_ceiling_warns_once_before_it_bites(tmp_path: Path) -> None:
    """FR-COST-06 — once. A warning per call is a warning nobody reads."""
    result = await pipeline(tmp_path, config=config(budget_usd=0.0375))
    warnings = [
        e for e in Session(tmp_path, result.run_id).read_events() if e["kind"] == "budget_warning"
    ]
    assert len(warnings) == 1
    assert warnings[0]["ceiling"] == 0.0375
    assert warnings[0]["remaining"] <= 0.0375 * 0.2 + 1e-9


def test_the_guard_adds_up_across_phases_not_per_phase() -> None:
    """One budget object, four providers. "Across all phases" has to be structural."""
    budget = RunBudget(0.02)
    guards = [BudgetGuard(FakeLLM([]), budget) for _ in PHASES]
    budget.add(0.01)
    budget.add(0.01)
    for guard in guards:
        with pytest.raises(BudgetExceeded):
            guard.budget.check()


def test_the_guard_passes_the_model_name_through() -> None:
    """The phase cache keys on it (FR-PIPE-05); a wrapper that hid it would key every run the
    same and serve a Sonnet artifact to a Qwen run."""

    class Named:
        model = "openrouter/qwen/qwen3-coder"

    assert BudgetGuard(Named(), RunBudget(1.0)).model == "openrouter/qwen/qwen3-coder"  # type: ignore[arg-type]


# --------------------------------------------------------------------------- FR-PIPE-04


async def test_a_phase_runs_standalone_from_artifacts_on_disk(tmp_path: Path) -> None:
    """`loom design` on a run that only got as far as plan."""
    first = await pipeline(tmp_path, [say(VALIDATION), say(PRD_OUT)], stop="plan")
    fake = FakeLLM([say(DESIGN_OUT)])
    result = await run_pipeline(
        "",
        root=tmp_path,
        provider_factory=lambda phase: fake,
        config=config(),
        session=Session(tmp_path, first.run_id),
        start="design",
        stop="design",
        cache=PhaseCache(tmp_path, enabled=False),
    )
    assert result.status == "passed"
    assert result.design is not None and result.design.summary.startswith("a slugify")
    assert fake.calls[0][1]["content"].count("shorten a URL") == 1


async def test_a_phase_with_no_upstream_artifact_says_which_one_is_missing(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError, match="plan"):
        await run_pipeline(
            "",
            root=tmp_path,
            provider_factory=lambda phase: FakeLLM([]),
            config=config(),
            start="design",
            stop="design",
        )


def test_the_phase_range_is_the_pipeline_order() -> None:
    assert phase_range() == list(PHASES)
    assert phase_range("plan", "design") == ["plan", "design"]
    assert phase_range("build", "build") == ["build"]
    with pytest.raises(ValueError, match="unknown phase"):
        phase_range("deploy")
    with pytest.raises(ValueError, match="comes after"):
        phase_range("design", "plan")


# --------------------------------------------------------------------------- FR-GATE-02


async def test_a_rejection_re_runs_the_phase_with_the_feedback(tmp_path: Path) -> None:
    fake = FakeLLM([say(VALIDATION), say(VALIDATION), say(PRD_OUT), say(DESIGN_OUT)])
    rejected: list[str] = []

    def gate(phase: str, artifact: Any, previous: Any = None) -> Decision:
        if phase == "validate" and not rejected:
            rejected.append(phase)
            return Decision(action="reject", feedback="you named no competitors")
        return Decision(action="approve")

    result = await pipeline(tmp_path, fake=fake, gate=gate)
    assert result.status == "passed"
    assert "you named no competitors" in fake.calls[1][1]["content"]


async def test_the_second_attempt_is_shown_against_the_first(tmp_path: Path) -> None:
    """FR-GATE-03 — the gate receives the previous artifact, not only the new one."""
    fake = FakeLLM(
        [say(VALIDATION), say({**VALIDATION, "verdict": "pivot"}), say(PRD_OUT), say(DESIGN_OUT)]
    )
    seen: list[Any] = []

    def gate(phase: str, artifact: Any, previous: Any = None) -> Decision:
        seen.append((phase, previous))
        if phase == "validate" and previous is None:
            return Decision(action="reject", feedback="reconsider")
        return Decision(action="approve")

    await pipeline(tmp_path, fake=fake, gate=gate)
    assert seen[1][0] == "validate"
    assert isinstance(seen[1][1], Validation) and seen[1][1].verdict == "go"


async def test_an_edit_is_used_and_the_phase_does_not_re_run(tmp_path: Path) -> None:
    fake = FakeLLM(list(SHAPE_A))
    edited = Validation(idea="mine", verdict="go", reasoning="I decided")

    def gate(phase: str, artifact: Any, previous: Any = None) -> Decision:
        if phase == "validate":
            return Decision(action="edit", artifact=edited)
        return Decision(action="approve")

    result = await pipeline(tmp_path, fake=fake, gate=gate)
    assert fake.call_count == 3  # not four: no re-run
    assert result.validation == edited
    on_disk = json.loads(Path(result.artifacts["validate"]).read_text())
    assert on_disk["reasoning"] == "I decided"
    assert "I decided" in fake.calls[1][1]["content"]  # the plan phase reads the edit


async def test_a_reviewer_who_keeps_rejecting_is_stopped(tmp_path: Path) -> None:
    """Two re-runs is enough to fix a real complaint; past that the answer is `edit`, not a
    third invoice."""
    fake = FakeLLM([say(VALIDATION)] * 6)
    result = await pipeline(
        tmp_path,
        fake=fake,
        gate=lambda phase, artifact, previous=None: Decision(action="reject", feedback="no"),
    )
    assert result.status == "abandoned"
    assert fake.call_count == 3


async def test_quitting_at_a_gate_stops_the_run(tmp_path: Path) -> None:
    fake = FakeLLM(list(SHAPE_A))
    result = await pipeline(
        tmp_path,
        fake=fake,
        gate=lambda phase, artifact, previous=None: Decision(action="abandon"),
    )
    assert result.status == "abandoned"
    assert fake.call_count == 1
    # The one phase that did run left its artifact behind, so the run is resumable.
    assert result.phases_run == ["validate"]
    assert Path(result.artifacts["validate"]).is_file()


# --------------------------------------------------------------------------- FR-VAL-04


async def test_a_no_go_verdict_stops_before_plan(tmp_path: Path) -> None:
    fake = FakeLLM([say({**VALIDATION, "verdict": "no-go", "reasoning": "nobody wants this"})])
    result = await pipeline(tmp_path, fake=fake)
    assert result.status == "no-go"
    assert result.phases_run == ["validate"]
    assert "nobody wants this" in result.message
    assert "--from plan" in result.message  # the override is named, not hidden
    assert fake.call_count == 1


async def test_a_pivot_verdict_does_not_stop_the_run(tmp_path: Path) -> None:
    """`pivot` is a redirection, not a refusal — the plan phase is told to plan the pivot."""
    fake = FakeLLM([say({**VALIDATION, "verdict": "pivot"}), say(PRD_OUT), say(DESIGN_OUT)])
    result = await pipeline(tmp_path, fake=fake)
    assert result.status == "passed"


async def test_no_go_can_be_overruled_by_starting_at_plan(tmp_path: Path) -> None:
    first = await pipeline(tmp_path, [say({**VALIDATION, "verdict": "no-go"})], stop="validate")
    assert first.status == "no-go"
    result = await run_pipeline(
        "",
        root=tmp_path,
        provider_factory=lambda phase: FakeLLM([say(PRD_OUT), say(DESIGN_OUT)]),
        config=config(),
        session=Session(tmp_path, first.run_id),
        start="plan",
        stop="design",
        cache=PhaseCache(tmp_path, enabled=False),
    )
    assert result.status == "passed"


# --------------------------------------------------------------------------- FR-HEADLESS


async def test_a_full_run_needs_no_tty(tmp_path: Path) -> None:
    """FR-HEADLESS-01 — the default gate is `AutoApprove`, which has nothing to attach to."""
    result = await pipeline(tmp_path)
    assert result.status == "passed"


async def test_unattended_without_a_budget_is_refused(tmp_path: Path) -> None:
    """FR-HEADLESS-04 — a model in a loop with no ceiling and nobody watching."""
    with pytest.raises(UnattendedWithoutBudget, match="--budget"):
        await pipeline(tmp_path, unattended=True)


async def test_unattended_with_a_budget_runs(tmp_path: Path) -> None:
    result = await pipeline(tmp_path, unattended=True, budget_usd=1.0)
    assert result.status == "passed"


# --------------------------------------------------------------------------- FR-PIPE-05


async def test_a_second_identical_run_spends_nothing(tmp_path: Path) -> None:
    cache = PhaseCache(tmp_path)
    await pipeline(tmp_path, cache=cache)
    fake = FakeLLM([])
    result = await pipeline(tmp_path, fake=fake, cache=cache)
    assert fake.call_count == 0
    assert result.status == "passed"
    assert result.usd_spent == 0.0


# --------------------------------------------------------------------------- the whole thing


@pytest.mark.slow
async def test_four_phases_produce_four_artifacts_and_four_approvals(tmp_path: Path) -> None:
    """FR-PIPE-01 end to end: the build phase included, against a real git repository."""
    Workspace.create(tmp_path)
    design = {
        **DESIGN_OUT,
        "rubric": {
            "threshold": 0.85,
            "hard_fail": [],
            "criteria": [
                shell("exists", 0.5, "test -f slugify.py"),
                {
                    "name": "tests",
                    "kind": "shell",
                    "weight": 0.5,
                    "command": "python3 -m pytest -q",
                },
            ],
        },
    }
    build_turns = [
        write("slugify.py", "x = 1\n"),
        write("test_slugify.py", "def test_ok():\n    assert True\n", "c2"),
    ]
    fake = FakeLLM(
        [
            say(VALIDATION),
            say(PRD_OUT),
            say(design),
            *build_turns,
            Response(text="done", usd_cost=0.01),
        ]
    )

    result = await run_pipeline(
        "a url shortener",
        root=tmp_path,
        provider_factory=lambda phase: fake,
        config=config(),
        cache=PhaseCache(tmp_path, enabled=False),
        fetcher=lambda url: (url, "<html>a page</html>"),
    )

    assert result.status == "passed"
    assert result.phases_run == list(PHASES)
    assert set(result.artifacts) == set(PHASES)
    assert Path(result.artifacts["build"]).name == "score.json"
    assert result.build is not None and result.build.score is not None
    assert result.build.score.total == 1.0

    approvals = [
        e for e in Session(tmp_path, result.run_id).read_events() if e["kind"] == "gate_decision"
    ]
    assert [e["phase"] for e in approvals] == list(PHASES)


@pytest.mark.slow
async def test_a_rejected_build_re_runs_with_the_feedback(tmp_path: Path) -> None:
    """FR-GATE-02 at the last gate: the complaint is about the code, not about the plan."""
    Workspace.create(tmp_path)
    design = {
        **DESIGN_OUT,
        "rubric": {
            "threshold": 0.5,
            "hard_fail": [],
            "criteria": [
                shell("exists", 0.5, "test -f slugify.py"),
                {
                    "name": "tests",
                    "kind": "shell",
                    "weight": 0.5,
                    "command": "python3 -m pytest -q",
                },
            ],
        },
    }
    made = write("slugify.py", "x = 1\n")
    done = Response(text="done", usd_cost=0.01)
    fake = FakeLLM([say(VALIDATION), say(PRD_OUT), say(design), made, done, made, done])
    rejected: list[str] = []

    def gate(phase: str, artifact: Any, previous: Any = None) -> Decision:
        if phase == "build" and not rejected:
            rejected.append(phase)
            return Decision(action="reject", feedback="no docstrings anywhere")
        return Decision(action="approve")

    result = await run_pipeline(
        "a url shortener",
        root=tmp_path,
        provider_factory=lambda phase: fake,
        config=config(),
        gate=gate,
        cache=PhaseCache(tmp_path, enabled=False),
        fetcher=lambda url: (url, "<html>x</html>"),
    )
    assert result.status == "passed"
    assert "no docstrings anywhere" in fake.calls[5][1]["content"]


@pytest.mark.slow
async def test_a_failed_build_reports_its_own_status(tmp_path: Path) -> None:
    """A run that reaches build and stalls is not "passed"; the exit code depends on it."""
    Workspace.create(tmp_path)
    design = {
        **DESIGN_OUT,
        "rubric": {
            "threshold": 0.9,
            "hard_fail": [],
            "criteria": [
                shell("exists", 0.5, "test -f nope.py"),
                {
                    "name": "tests",
                    "kind": "shell",
                    "weight": 0.5,
                    "command": "python3 -m pytest nope/ -q",
                },
            ],
        },
    }
    done = Response(text="done", usd_cost=0.01)
    fake = FakeLLM([say(VALIDATION), say(PRD_OUT), say(design), done, done, done, done])
    result = await run_pipeline(
        "a url shortener",
        root=tmp_path,
        provider_factory=lambda phase: fake,
        config=config(max_turns=4),
        cache=PhaseCache(tmp_path, enabled=False),
        fetcher=lambda url: (url, "<html>x</html>"),
    )
    assert result.status in {"stalled", "budget_exhausted"}
    assert result.build is not None and not result.build.passed
    assert result.build.snapshot  # FR-BUILD-02 — something to resume from


async def test_a_research_phase_does_not_get_the_build_s_turn_budget(tmp_path: Path) -> None:
    """A real run gave validate forty turns and it spent sixteen searching. A phase that
    cannot write a file has no use for a cap sized for one that can."""
    fake = FakeLLM(list(SHAPE_A))
    await pipeline(tmp_path, fake=fake, config=config(max_turns=40))
    # The loop is handed the cap, so assert on what the phase was actually allowed.
    from loom.phases.base import MAX_ATTEMPTS
    from loom.pipeline import SHAPE_A_TURNS

    assert SHAPE_A_TURNS < 40
    searching = FakeLLM(
        [
            Response(
                tool_calls=[ToolCall(id=f"c{i}", name="search_web", arguments={"query": "again"})],
                usd_cost=0.001,
            )
            for i in range(SHAPE_A_TURNS + 5)
        ]
    )
    result = await pipeline(tmp_path, fake=searching, stop="validate", config=config(max_turns=40))
    assert result.status == "invalid"  # ran out of turns, no artifact
    # The *loop* is capped at SHAPE_A_TURNS. With money still to spare it then spends up to
    # MAX_ATTEMPTS-1 tool-free salvage calls, asking the model to turn its research into the
    # artifact; this model never concludes, so they are spent in vain and the phase stays
    # 'invalid'. The real model (WP-4.7) produces its JSON on the first of them.
    assert searching.call_count == SHAPE_A_TURNS + (MAX_ATTEMPTS - 1)
