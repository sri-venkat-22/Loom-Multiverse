"""FR-BUILD-01/02/03/05, FR-ART-04, SEC-08 — the build phase against FakeLLM, no network.

The four exits in the done-when get one test each: threshold reached, sub-threshold re-prompt,
budget exhaustion with a resumable label, and a stall that does not burn the budget.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from loom.contracts import Criterion, Design, FileSpec, Response, Rubric, ToolCall
from loom.ledger import Ledger
from loom.phases.build import PROMPT, BuildResult, RubricGrader, run_build
from loom.testing.fake_llm import FakeLLM
from loom.workspace import Workspace

#: NFR-TEST-01 — every test here drives a real git repository, so the file costs
#: seconds rather than milliseconds. `pytest -m "not slow"` is the on-save tier;
#: plain `pytest` still runs all of it.
pytestmark = pytest.mark.slow

SLUGIFY = '''def slugify(text: str) -> str:
    """Turn text into a url-safe slug."""
    kept = [c if c.isalnum() else "-" for c in text.lower().strip()]
    return "-".join(part for part in "".join(kept).split("-") if part)
'''

SUITE = """from slugify import slugify


def test_basic():
    assert slugify("Hello World") == "hello-world"


def test_repeated_separators():
    assert slugify("a   b") == "a-b"
"""


def design(
    *,
    criteria: list[Criterion] | None = None,
    threshold: float = 0.85,
    hard_fail: list[str] | None = None,
    scaffold_command: str | None = None,
) -> Design:
    return Design(
        summary="a slugify library with tests",
        stack=["python"],
        scaffold_command=scaffold_command,
        file_manifest=[
            FileSpec(path="slugify.py", purpose="the slugify function"),
            FileSpec(path="test_slugify.py", purpose="its tests", depends_on=["slugify.py"]),
        ],
        rubric=Rubric(
            criteria=criteria
            or [Criterion(name="tests", kind="shell", weight=1.0, command="python3 -m pytest -q")],
            threshold=threshold,
            hard_fail=hard_fail or [],
        ),
    )


#: Rubrics that decide without running a test suite. Only the two tests that assert on real
#: failure output pay for a real `pytest` subprocess; the rest are about wiring, and a build
#: phase test that spends half a second proving pytest works is testing pytest.
TRIVIAL = [Criterion(name="ok", kind="shell", weight=1.0, command="exit 0")]
NEVER = [Criterion(name="impossible", kind="shell", weight=1.0, command="exit 1")]


def write(path: str, content: str, cid: str = "c1") -> Response:
    return Response(
        tool_calls=[
            ToolCall(id=cid, name="write_file", arguments={"path": path, "content": content})
        ],
        usd_cost=0.01,
    )


def bash(command: str, cid: str = "c1") -> Response:
    return Response(
        tool_calls=[ToolCall(id=cid, name="run_bash", arguments={"command": command})],
        usd_cost=0.01,
    )


def working_build() -> list[Response]:
    return [
        write("slugify.py", SLUGIFY, "c1"),
        write("test_slugify.py", SUITE, "c2"),
        bash("python3 -m pytest -q", "c3"),
        Response(text="Built slugify.py and its tests; the suite passes.", usd_cost=0.01),
    ]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    Workspace.create(tmp_path)
    return tmp_path


def log(root: Path) -> str:
    return subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True
    ).stdout


# --------------------------------------------------------------------------- FR-BUILD-01/02


async def test_a_repo_that_clears_its_rubric_exits_passed(repo: Path) -> None:
    """The whole product, in one test, with the model scripted: files written, suite run,
    rubric graded, phase committed."""
    fake = FakeLLM(working_build())
    result = await run_build(design(), workspace=repo, provider=fake)

    assert result.status == "passed"
    assert result.score is not None and result.score.total == 1.0
    assert result.rounds == 1
    assert (repo / "slugify.py").is_file() and (repo / "test_slugify.py").is_file()
    assert result.usd_spent == pytest.approx(0.04)


async def test_a_sub_threshold_score_re_prompts_with_the_actual_failure(repo: Path) -> None:
    """FR-BUILD-03 — "still fails" is not something a model can act on."""
    broken = "def slugify(text):\n    return 'WRONG'\n"
    fake = FakeLLM(
        [
            write("slugify.py", broken, "c1"),
            write("test_slugify.py", SUITE, "c2"),
            Response(text="done", usd_cost=0.01),  # graded: the suite fails
            write("slugify.py", SLUGIFY, "c3"),  # ...so it fixes it
            Response(text="fixed", usd_cost=0.01),  # graded again: passes
        ]
    )
    result = await run_build(design(), workspace=repo, provider=fake)

    assert result.status == "passed"
    assert result.rounds == 2

    feedback = [m for m in fake.calls[-1] if m["role"] == "user"][-1]["content"]
    assert "Rubric score 0.00" in feedback
    assert "tests" in feedback
    assert "assert" in feedback or "Error" in feedback, "the model was told nothing useful"


async def test_budget_exhaustion_leaves_partial_state_and_a_resumable_label(repo: Path) -> None:
    """FR-BUILD-02 — cleanly, with the work so far still on disk and a label to come back to."""
    fake = FakeLLM([write("slugify.py", SLUGIFY, f"c{i}") for i in range(6)])
    result = await run_build(design(), workspace=repo, provider=fake, max_turns=3)

    assert result.status == "budget_exhausted"
    assert result.turns_used == 3
    assert result.snapshot == "turn-3"
    assert (repo / "slugify.py").is_file(), "the partial work was thrown away"

    reopened = Workspace.open(repo)
    assert reopened.resolve(result.snapshot)
    assert result.commit is None, "an unfinished phase must not be squashed away"


async def test_a_usd_ceiling_stops_the_build(repo: Path) -> None:
    fake = FakeLLM([write("slugify.py", SLUGIFY, f"c{i}") for i in range(6)])
    result = await run_build(design(), workspace=repo, provider=fake, max_usd=0.025)
    assert result.status == "budget_exhausted"
    assert fake.call_count == 3, "it paid for a call it had already decided not to use"


async def test_a_rubric_that_never_improves_stalls_instead_of_burning_the_budget(
    repo: Path,
) -> None:
    """FR-BUILD-02 — 4 graded rounds (one baseline, three flat), not 40 turns."""
    fake = FakeLLM([Response(text=f"attempt {i}", usd_cost=0.01) for i in range(40)])
    result = await run_build(design(criteria=NEVER), workspace=repo, provider=fake, max_turns=40)

    assert result.status == "stalled"
    assert fake.call_count == 4
    assert result.rounds == 4
    assert result.usd_spent == pytest.approx(0.04)


async def test_an_empty_response_exits_blocked(repo: Path) -> None:
    result = await run_build(design(), workspace=repo, provider=FakeLLM([Response()]))
    assert result.status == "blocked"


# --------------------------------------------------------------------------- FR-BUILD-05, SEC-08


async def test_every_turn_leaves_a_revertible_snapshot(repo: Path) -> None:
    fake = FakeLLM(working_build())
    result = await run_build(design(criteria=TRIVIAL), workspace=repo, provider=fake)

    workspace = Workspace.open(repo)
    for turn in range(1, result.turns_used + 1):
        assert workspace.resolve(f"turn-{turn}")
    assert workspace.resolve("scaffold")

    workspace.reset_to("turn-1")  # after the first write_file, before the tests existed
    assert (repo / "slugify.py").is_file()
    assert not (repo / "test_slugify.py").exists()


async def test_success_squashes_the_phase_into_one_commit(repo: Path) -> None:
    before = len(log(repo).splitlines())
    result = await run_build(
        design(criteria=TRIVIAL), workspace=repo, provider=FakeLLM(working_build())
    )

    assert result.commit is not None
    assert len(log(repo).splitlines()) == before + 1
    assert "slugify" in log(repo).splitlines()[0]
    assert not Workspace.open(repo).is_dirty()


async def test_the_snapshots_survive_the_squash(repo: Path) -> None:
    """A squashed phase is still rewindable — the labels outlive the branch history."""
    await run_build(design(criteria=TRIVIAL), workspace=repo, provider=FakeLLM(working_build()))
    workspace = Workspace.open(repo)
    workspace.reset_to("turn-1")
    assert not (repo / "test_slugify.py").exists()


async def test_it_refuses_to_build_over_uncommitted_work(repo: Path) -> None:
    from loom.workspace import DirtyWorkspace

    (repo / "someones_afternoon.py").write_text("half a thought", encoding="utf-8")
    with pytest.raises(DirtyWorkspace):
        await run_build(design(), workspace=repo, provider=FakeLLM(working_build()))


# --------------------------------------------------------------------------- scaffold, FR-ART-04


async def test_the_scaffold_runs_before_the_first_turn(repo: Path) -> None:
    fake = FakeLLM([bash("cat scaffolded.txt", "c1"), Response(text="done", usd_cost=0.01)])
    result = await run_build(
        design(criteria=TRIVIAL, scaffold_command="echo ready > scaffolded.txt"),
        workspace=repo,
        provider=fake,
    )
    assert result.scaffold is not None and result.scaffold.ok
    tool_result = [m for m in fake.calls[-1] if m["role"] == "tool"][0]["content"]
    assert "ready" in tool_result, "the agent's first turn could not see the scaffold"


async def test_a_failed_scaffold_does_not_stop_the_build(repo: Path) -> None:
    result = await run_build(
        design(criteria=TRIVIAL, scaffold_command="exit 2"),
        workspace=repo,
        provider=FakeLLM(working_build()),
    )
    assert result.scaffold is not None and result.scaffold.ok is False
    assert result.status == "passed", "a bad scaffold command should cost a turn, not the run"


async def test_the_score_is_persisted_for_inspection(repo: Path) -> None:
    """FR-ART-04."""
    result = await run_build(
        design(criteria=TRIVIAL),
        workspace=repo,
        provider=FakeLLM(working_build()),
        run_id="20260831T101500-abc",
    )
    assert result.score_path == repo / ".loom/artifacts/20260831T101500-abc/score.json"
    saved = json.loads(result.score_path.read_text(encoding="utf-8"))
    assert saved["total"] == 1.0
    assert saved["criteria"][0]["name"] == "ok"


async def test_the_persisted_score_is_the_failing_one_too(repo: Path) -> None:
    fake = FakeLLM([Response(text=f"attempt {i}", usd_cost=0.01) for i in range(10)])
    result = await run_build(design(criteria=NEVER), workspace=repo, provider=fake, max_turns=40)
    assert result.score_path is not None
    assert json.loads(result.score_path.read_text(encoding="utf-8"))["total"] == 0.0


# --------------------------------------------------------------------------- wiring


async def test_the_agent_gets_the_build_tools_and_only_those(repo: Path) -> None:
    fake = FakeLLM([Response(text="nothing to do", usd_cost=0.01)])
    await run_build(design(criteria=TRIVIAL), workspace=repo, provider=fake)
    assert fake.tool_names_seen(0) == [
        "read_file",
        "write_file",
        "str_replace",
        "run_bash",
        "ask_user",
    ]


async def test_the_task_shows_the_design_and_the_rubric(repo: Path) -> None:
    # Padded: this rubric fails, so the loop grades and re-prompts until it stalls. The test
    # is about what the model was told on call 0, not about how the build ends.
    fake = FakeLLM([Response(text="ok", usd_cost=0.01) for _ in range(6)])
    await run_build(
        design(
            criteria=[
                Criterion(name="tests", kind="shell", weight=0.7, command="python3 -m pytest -q"),
                Criterion(name="readable", kind="judge", weight=0.3, question="Is it readable?"),
            ],
            hard_fail=["tests"],
        ),
        workspace=repo,
        provider=fake,
        judge_provider=FakeJudge(),
    )
    task = fake.calls[0][1]["content"]
    assert "a slugify library with tests" in task
    assert "`slugify.py`" in task and "the slugify function" in task
    assert "after slugify.py" in task
    assert "python3 -m pytest -q" in task
    assert "[HARD FAIL]" in task
    assert "threshold 0.85" in task


class FakeJudge:
    async def complete(self, messages: Any, tools: Any = None) -> Response:
        return Response(
            text='{"score": 1.0, "reason": "fine"}', usd_cost=0.0004, in_tokens=100, out_tokens=10
        )


async def test_judge_spend_is_ledgered_apart_from_the_build(repo: Path) -> None:
    ledger = Ledger(repo / ".loom" / "ledger.db")
    ledger.record(phase="build", model="qwen", usd=0.04, run_id="r1")
    await run_build(
        design(
            criteria=[
                Criterion(name="readable", kind="judge", weight=1.0, question="Is it readable?")
            ]
        ),
        workspace=repo,
        provider=FakeLLM([Response(text="ok", usd_cost=0.01)]),
        judge_provider=FakeJudge(),
        ledger=ledger,
        judge_model="cheap-judge",
        run_id="r1",
    )
    assert set(ledger.by_phase()) == {"build", "judge"}


async def test_the_phase_narrates_itself_through_events(repo: Path) -> None:
    """NFR-OBS-01 — a piped run loses nothing."""
    events: list[tuple[str, dict[str, Any]]] = []
    await run_build(
        design(criteria=TRIVIAL),
        workspace=repo,
        provider=FakeLLM(working_build()),
        on_event=lambda kind, **f: events.append((kind, f)),
    )
    kinds = [k for k, _ in events]
    assert kinds[0] == "phase_started"
    assert kinds[-1] == "phase_finished"
    assert "graded" in kinds and "artifact_written" in kinds
    finished = events[-1][1]
    assert finished["status"] == "passed" and finished["total"] == 1.0
    assert finished["snapshot"] == "turn-4"


async def test_the_memo_survives_across_grading_rounds(repo: Path) -> None:
    """COST CONTROL 2 only means anything if the same memo is reused, which is why the grader
    is an object rather than a closure."""
    grader = RubricGrader(design(criteria=TRIVIAL).rubric, workspace=repo)
    await grader.grade()
    assert set(grader.memo) == {"ok"}
    await grader.grade()
    assert grader.rounds == 2


# --------------------------------------------------------------------------- FR-BUILD-04


def test_the_prompt_carries_an_anti_gaming_clause() -> None:
    """FR-BUILD-04 — the rubric measures the product, it is not the product. An eval fixture
    that tempts the model is the real verification; this asserts the clause is there at all."""
    prompt = PROMPT.read_text(encoding="utf-8").lower()
    assert "integrity" in prompt
    for temptation in ("skip", "xfail", "delete", "loosen", "special-case"):
        assert temptation in prompt, f"the clause does not mention {temptation}"
    assert "failed build" in prompt
    assert "rubric" in prompt and "not the product" in prompt


def test_build_result_reports_its_own_verdict() -> None:
    assert BuildResult(status="passed").passed
    assert not BuildResult(status="stalled").passed
