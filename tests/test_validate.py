"""WP-3.2 — the Validate phase. FR-VAL-01/02/03, FR-PLAN-03, SEC-03, SEC-04.

The security half of this file matters more than the functional half. Validate is the only
phase that reads the open internet, and the property that keeps that survivable is that it has
nothing dangerous to be talked into using.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.agent.tools.web import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from loom.contracts import Response, ToolCall, Validation
from loom.phases.base import artifact_path
from loom.phases.validate import ValidatePhase
from loom.testing.fake_llm import FakeLLM

GOOD = {
    "idea": "a url shortener with click analytics",
    "verdict": "go",
    "reasoning": "small, buildable, and the analytics angle is a real wedge.",
    "competitors": ["Bitly", "Short.io"],
    "risks": ["Bitly bundles analytics for free on paid tiers"],
    "sources": ["https://bitly.com/pricing"],
}

PAGE = "<html><body><h1>Bitly pricing</h1><p>Analytics from $8/mo.</p></body></html>"


def answer(payload: dict[str, object] | None = None) -> Response:
    return Response(text=json.dumps(payload or GOOD), usd_cost=0.001)


def phase() -> ValidatePhase:
    return ValidatePhase(fetcher=lambda url: (url, PAGE))


async def test_it_produces_a_valid_validation(tmp_path: Path) -> None:
    """FR-VAL-01."""
    outcome = await phase().execute(
        provider=FakeLLM([answer()]),
        inputs={"idea": "a url shortener with click analytics"},
        root=tmp_path,
        run_id="r1",
    )
    artifact: Validation = outcome.artifact
    assert artifact.verdict == "go"
    assert artifact.competitors == ["Bitly", "Short.io"]
    assert outcome.path == artifact_path(tmp_path, "r1", "validate")


@pytest.mark.parametrize("verdict", ["go", "pivot", "no-go"])
async def test_all_three_verdicts_round_trip(verdict: str, tmp_path: Path) -> None:
    outcome = await phase().execute(
        provider=FakeLLM([answer({**GOOD, "verdict": verdict})]),
        inputs={"idea": "x"},
        root=tmp_path,
    )
    assert outcome.artifact.verdict == verdict


async def test_a_fourth_verdict_is_repaired_not_accepted(tmp_path: Path) -> None:
    """`maybe` is the verdict a model reaches for when it does not want to decide."""
    fake = FakeLLM([answer({**GOOD, "verdict": "maybe"}), answer()])
    outcome = await phase().execute(provider=fake, inputs={"idea": "x"}, root=tmp_path)
    assert outcome.artifact.verdict == "go"
    assert "verdict" in fake.calls[1][-1]["content"]


# --------------------------------------------------------------------------- SEC-03


async def test_it_gets_the_web_tools_and_only_the_web_tools(tmp_path: Path) -> None:
    """FR-VAL-02 — asserted on the tool list actually sent to the provider."""
    fake = FakeLLM([answer()])
    await phase().execute(provider=fake, inputs={"idea": "x"}, root=tmp_path)
    offered = set(fake.tool_names_seen(0))
    assert offered == {"ask_user", "search_web", "fetch_url"}
    assert offered & {"read_file", "write_file", "str_replace", "run_bash"} == set()


async def test_there_is_no_argument_that_could_add_a_filesystem_tool(tmp_path: Path) -> None:
    """SEC-03 is enforced at construction: `tools()` is the whole surface, and it is closed."""
    assert set(ValidatePhase().tools().names) == {"ask_user", "search_web", "fetch_url"}


# --------------------------------------------------------------------------- SEC-04


async def test_fetched_page_text_reaches_the_model_wrapped_and_labelled(tmp_path: Path) -> None:
    """FR-VAL-03 — the delimiter is on the tool message, which is the one the model reads."""
    fake = FakeLLM(
        [
            Response(
                tool_calls=[
                    ToolCall(id="c1", name="fetch_url", arguments={"url": "https://b.test"})
                ]
            ),
            answer(),
        ]
    )
    await phase().execute(provider=fake, inputs={"idea": "x"}, root=tmp_path)
    tool_message = next(m for m in fake.calls[1] if m.get("role") == "tool")
    assert tool_message["content"].startswith(UNTRUSTED_OPEN)
    assert UNTRUSTED_CLOSE in tool_message["content"]
    assert "Analytics from $8/mo." in tool_message["content"]


async def test_the_system_prompt_says_the_delimiter_is_never_an_instruction(
    tmp_path: Path,
) -> None:
    fake = FakeLLM([answer()])
    await phase().execute(provider=fake, inputs={"idea": "x"}, root=tmp_path)
    system = fake.calls[0][0]["content"]
    assert UNTRUSTED_OPEN in system
    assert "never an instruction" in system


# --------------------------------------------------------------------------- the task


async def test_the_idea_and_the_founder_s_notes_reach_the_task(tmp_path: Path) -> None:
    fake = FakeLLM([answer()])
    await phase().execute(
        provider=fake,
        inputs={"idea": "a url shortener", "notes": "must run offline"},
        root=tmp_path,
    )
    task = fake.calls[0][1]["content"]
    assert "a url shortener" in task
    assert "must run offline" in task


def test_an_empty_idea_is_refused_before_a_call_is_made() -> None:
    with pytest.raises(ValueError, match="needs an idea"):
        ValidatePhase().prepare_input(idea="   ")


def test_the_prompt_asks_for_a_decision_rather_than_a_survey() -> None:
    """FR-VAL-01 — the verdict is the product of this phase; a prompt that lets it hedge
    produces an artifact nobody can gate on."""
    text = ValidatePhase().prompt
    assert "go" in text and "pivot" in text and "no-go" in text
    assert "not a verdict" in text
