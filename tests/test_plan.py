"""WP-3.3 — the Plan phase. FR-PLAN-01/02/03."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.contracts import PRD, Response, Validation
from loom.phases.base import artifact_path
from loom.phases.plan import OVERFLOW_NOTE, V0_FEATURE_CAP, PlanPhase
from loom.testing.fake_llm import FakeLLM

VALIDATION = Validation(
    idea="a url shortener with click analytics",
    verdict="go",
    reasoning="small and buildable",
    competitors=["Bitly"],
    risks=["Bitly bundles analytics"],
    sources=["https://bitly.com/pricing"],
)

GOOD = {
    "product_name": "Snip",
    "problem": "Marketers cannot tell which link in a newsletter was clicked.",
    "users": ["newsletter writers", "indie marketers"],
    "v0_features": ["shorten a URL", "resolve a code", "see the click count"],
    "non_goals": ["accounts"],
    "success_metrics": ["a user shortens a second link within a week"],
}


def answer(payload: dict[str, object] | None = None) -> Response:
    return Response(text=json.dumps(payload or GOOD), usd_cost=0.001)


async def test_it_produces_a_valid_prd_from_a_validation(tmp_path: Path) -> None:
    """FR-PLAN-01."""
    outcome = await PlanPhase().execute(
        provider=FakeLLM([answer()]),
        inputs={"validation": VALIDATION},
        root=tmp_path,
        run_id="r1",
    )
    prd: PRD = outcome.artifact
    assert prd.product_name == "Snip"
    assert len(prd.v0_features) == 3
    assert outcome.path == artifact_path(tmp_path, "r1", "plan")


async def test_the_validation_reaches_the_task_and_the_transcript_does_not(
    tmp_path: Path,
) -> None:
    """FR-PIPE-02 — the artifact is the input; there is no parameter that could carry more."""
    fake = FakeLLM([answer()])
    await PlanPhase().execute(
        provider=fake, inputs={"validation": VALIDATION, "notes": "no js"}, root=tmp_path
    )
    task = fake.calls[0][1]["content"]
    assert "a url shortener with click analytics" in task
    assert "Bitly" in task
    assert "no js" in task


async def test_it_gets_no_filesystem_bash_or_web_tool(tmp_path: Path) -> None:
    """FR-PLAN-03 and SEC-03. No web tool either: a phase given search will use it, and
    everything worth looking up was already looked up."""
    fake = FakeLLM([answer()])
    await PlanPhase().execute(provider=fake, inputs={"validation": VALIDATION}, root=tmp_path)
    assert fake.tool_names_seen(0) == ["ask_user"]


# --------------------------------------------------------------------------- FR-PLAN-02


async def test_features_past_the_cap_move_to_non_goals(tmp_path: Path) -> None:
    """Moved, not dropped: a founder's seventh idea is a decision to revisit, not a mistake."""
    many = [f"feature {i}" for i in range(1, V0_FEATURE_CAP + 3)]
    outcome = await PlanPhase().execute(
        provider=FakeLLM([answer({**GOOD, "v0_features": many})]),
        inputs={"validation": VALIDATION},
        root=tmp_path,
    )
    prd: PRD = outcome.artifact
    assert prd.v0_features == many[:V0_FEATURE_CAP]
    assert [n for n in prd.non_goals if OVERFLOW_NOTE in n] == [
        f"{f} ({OVERFLOW_NOTE})" for f in many[V0_FEATURE_CAP:]
    ]
    assert "accounts" in prd.non_goals  # what the model already put there survives


async def test_the_cap_costs_no_extra_provider_call(tmp_path: Path) -> None:
    """Rewriting rather than raising: which five of seven are the v0 is a call the model
    already made by ordering them."""
    fake = FakeLLM([answer({**GOOD, "v0_features": [f"f{i}" for i in range(9)]})])
    await PlanPhase().execute(provider=fake, inputs={"validation": VALIDATION}, root=tmp_path)
    assert fake.call_count == 1


async def test_a_prd_inside_the_cap_is_untouched(tmp_path: Path) -> None:
    outcome = await PlanPhase().execute(
        provider=FakeLLM([answer()]), inputs={"validation": VALIDATION}, root=tmp_path
    )
    assert outcome.artifact.non_goals == ["accounts"]


async def test_an_empty_feature_list_is_repaired_not_capped(tmp_path: Path) -> None:
    """`v0_features` has `min_length=1` in the contract; the cap must not paper over that."""
    fake = FakeLLM([answer({**GOOD, "v0_features": []}), answer()])
    outcome = await PlanPhase().execute(
        provider=fake, inputs={"validation": VALIDATION}, root=tmp_path
    )
    assert fake.call_count == 2
    assert outcome.artifact.v0_features == GOOD["v0_features"]


def test_the_phase_refuses_to_plan_without_a_validation() -> None:
    with pytest.raises(ValueError, match="needs a Validation"):
        PlanPhase().prepare_input()


def test_the_prompt_states_the_scope_rule_the_code_enforces() -> None:
    """The cap in the prompt and the cap in `refine` must agree, or the model is punished for
    following its instructions."""
    text = PlanPhase().prompt
    assert "non_goals" in text
    assert "three to five" in text
