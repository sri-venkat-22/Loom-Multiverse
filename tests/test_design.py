"""WP-3.4 — the Design phase. FR-DES-01/02/03/05.

The Design is the only artifact the build reads, so every check here is really a check on what
a build round will be told to do. The hand-written fixture is the ground truth: a rule this
phase enforces that the fixture would fail is a rule that is wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.contracts import PRD, Design, Response, Validation
from loom.phases.base import artifact_path
from loom.phases.design import MIN_CRITERIA, TEST_RUNNERS, DesignPhase
from loom.testing.fake_llm import FakeLLM

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "urlshortener" / "design.json"

PRD_IN = PRD(
    product_name="Snip",
    problem="Marketers cannot tell which link was clicked.",
    users=["newsletter writers"],
    v0_features=["shorten a URL", "resolve a code", "see the click count"],
    non_goals=["accounts"],
    success_metrics=["a second link within a week"],
)

VALIDATION = Validation(
    idea="a url shortener", verdict="go", reasoning="buildable", risks=["Bitly bundles it"]
)

GOOD: dict[str, object] = {
    "summary": "a url shortener library with click analytics",
    "stack": ["Python 3.11", "sqlite3", "pytest"],
    "scaffold_command": "uv init -q",
    "file_manifest": [
        {"path": "src/snip/store.py", "purpose": "Store(path) with add/url_for/click_count."},
        {
            "path": "tests/test_snip.py",
            "purpose": "covers unknown code, no scheme, repeat shorten",
            "depends_on": ["src/snip/store.py"],
        },
    ],
    "rubric": {
        "threshold": 0.85,
        "hard_fail": ["tests"],
        "criteria": [
            {"name": "tests", "kind": "shell", "weight": 0.6, "command": "uv run pytest -q"},
            {
                "name": "readable",
                "kind": "judge",
                "weight": 0.4,
                "question": "Is it clear? 1.0 for type hints and docstrings, 0.0 for neither.",
                "evidence_paths": ["src/snip/*.py"],
            },
        ],
    },
}


def answer(payload: dict[str, object] | None = None) -> Response:
    return Response(text=json.dumps(payload or GOOD), usd_cost=0.001)


def rubric_with(**over: object) -> dict[str, object]:
    rubric = json.loads(json.dumps(GOOD["rubric"]))
    rubric.update(over)
    return {**GOOD, "rubric": rubric}


async def test_it_produces_a_valid_design(tmp_path: Path) -> None:
    """FR-DES-01."""
    outcome = await DesignPhase().execute(
        provider=FakeLLM([answer()]), inputs={"prd": PRD_IN}, root=tmp_path, run_id="r1"
    )
    design: Design = outcome.artifact
    assert design.rubric.threshold == 0.85
    assert [f.path for f in design.file_manifest] == ["src/snip/store.py", "tests/test_snip.py"]
    assert outcome.path == artifact_path(tmp_path, "r1", "design")


async def test_it_gets_no_filesystem_bash_or_web_tool(tmp_path: Path) -> None:
    """FR-DES-05, SEC-03."""
    fake = FakeLLM([answer()])
    await DesignPhase().execute(provider=fake, inputs={"prd": PRD_IN}, root=tmp_path)
    assert fake.tool_names_seen(0) == ["ask_user"]


async def test_the_prd_and_the_risks_reach_the_task(tmp_path: Path) -> None:
    fake = FakeLLM([answer()])
    await DesignPhase().execute(
        provider=fake, inputs={"prd": PRD_IN, "validation": VALIDATION}, root=tmp_path
    )
    task = fake.calls[0][1]["content"]
    assert "shorten a URL" in task
    assert "accounts" in task  # the non-goals, so the design does not re-add them
    assert "Bitly bundles it" in task


# --------------------------------------------------------------------------- FR-DES-03


async def test_a_single_criterion_rubric_is_sent_back(tmp_path: Path) -> None:
    one = rubric_with(
        criteria=[{"name": "tests", "kind": "shell", "weight": 1.0, "command": "pytest"}],
        hard_fail=[],
    )
    fake = FakeLLM([answer(one), answer()])
    outcome = await DesignPhase().execute(provider=fake, inputs={"prd": PRD_IN}, root=tmp_path)
    assert fake.call_count == 2
    assert f"at least {MIN_CRITERIA}" in fake.calls[1][-1]["content"]
    assert len(outcome.artifact.rubric.criteria) == 2


async def test_weights_that_do_not_sum_to_one_are_sent_back(tmp_path: Path) -> None:
    """A threshold of 0.85 against a possible total of 0.6 is a build that can never finish."""
    skewed = rubric_with(
        criteria=[
            {"name": "tests", "kind": "shell", "weight": 0.3, "command": "uv run pytest -q"},
            {"name": "readable", "kind": "judge", "weight": 0.3, "question": "clear?"},
        ]
    )
    fake = FakeLLM([answer(skewed), answer()])
    await DesignPhase().execute(provider=fake, inputs={"prd": PRD_IN}, root=tmp_path)
    complaint = fake.calls[1][-1]["content"]
    assert "sum to 0.600" in complaint
    assert "tests=0.3" in complaint  # it is told which weights, not just that they are wrong


async def test_rounding_is_not_treated_as_a_mistake(tmp_path: Path) -> None:
    """0.33 + 0.33 + 0.34 is a correct rubric badly typed; charging for a repair round would
    be us being pedantic with someone else's money."""
    thirds = rubric_with(
        hard_fail=[],
        criteria=[
            {"name": "tests", "kind": "shell", "weight": 0.33, "command": "uv run pytest -q"},
            {"name": "api", "kind": "shell", "weight": 0.33, "command": "python -c 'import x'"},
            {"name": "readable", "kind": "judge", "weight": 0.33, "question": "clear?"},
        ],
    )
    fake = FakeLLM([answer(thirds)])
    await DesignPhase().execute(provider=fake, inputs={"prd": PRD_IN}, root=tmp_path)
    assert fake.call_count == 1


async def test_a_rubric_that_never_runs_the_tests_is_sent_back(tmp_path: Path) -> None:
    """The one criterion a generated repo cannot be trusted without."""
    no_tests = rubric_with(
        hard_fail=[],
        criteria=[
            {"name": "imports", "kind": "shell", "weight": 0.5, "command": "python -c 'import x'"},
            {"name": "readable", "kind": "judge", "weight": 0.5, "question": "clear?"},
        ],
    )
    fake = FakeLLM([answer(no_tests), answer()])
    await DesignPhase().execute(provider=fake, inputs={"prd": PRD_IN}, root=tmp_path)
    complaint = fake.calls[1][-1]["content"]
    assert "test suite" in complaint
    assert TEST_RUNNERS[0] in complaint


@pytest.mark.parametrize("runner", TEST_RUNNERS)
def test_every_named_runner_is_recognised(runner: str) -> None:
    from loom.phases.design import _runs_a_test_suite

    assert _runs_a_test_suite(f"cd . && {runner.upper()} -q")
    assert not _runs_a_test_suite("echo hello")
    assert not _runs_a_test_suite(None)


# --------------------------------------------------------------------------- FR-DES-02


async def test_a_manifest_cycle_is_sent_back(tmp_path: Path) -> None:
    """Enforced in `contracts.py`; asserted here because the repair loop is what makes it
    survivable rather than fatal."""
    cyclic = {
        **GOOD,
        "file_manifest": [
            {"path": "a.py", "purpose": "a", "depends_on": ["b.py"]},
            {"path": "b.py", "purpose": "b", "depends_on": ["a.py"]},
        ],
    }
    fake = FakeLLM([answer(cyclic), answer()])
    await DesignPhase().execute(provider=fake, inputs={"prd": PRD_IN}, root=tmp_path)
    assert "cycle" in fake.calls[1][-1]["content"]


async def test_a_hard_fail_naming_nothing_is_sent_back(tmp_path: Path) -> None:
    fake = FakeLLM([answer(rubric_with(hard_fail=["typos"])), answer()])
    await DesignPhase().execute(provider=fake, inputs={"prd": PRD_IN}, root=tmp_path)
    assert "typos" in fake.calls[1][-1]["content"]


# --------------------------------------------------------------------------- FR-DES-04


def test_the_hand_written_fixture_clears_this_phase_s_own_gate() -> None:
    """The fixture is the target `prompts/design.md` is tuned to reproduce (FR-DES-04). A rule
    here that the fixture fails is a rule that is wrong, and this is the test that says so."""
    fixture = Design.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    assert DesignPhase().refine(fixture) is fixture


def test_the_prompt_asks_for_what_the_fixture_actually_does() -> None:
    """The gap between the prompt and the fixture is the gap FR-DES-04 measures in the eval
    harness. These are the specific habits that make the fixture gradable."""
    text = DesignPhase().prompt
    assert "acceptance check" in text  # the criterion the builder cannot weaken
    assert "evidence_paths" in text
    assert "hard_fail" in text
    assert "0.85" in text
    assert "contract, not a label" in text  # the file_manifest lesson from WP-4.5


def test_the_phase_refuses_to_design_without_a_prd() -> None:
    with pytest.raises(ValueError, match="needs a PRD"):
        DesignPhase().prepare_input()
