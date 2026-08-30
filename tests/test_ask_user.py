"""FR-ASK-01…05 — one question, a cap, a default, and an event for every word of it."""

from __future__ import annotations

from typing import Any

import pytest

from loom.agent.loop import run_agent_loop
from loom.agent.tools.ask_user import CAP, CAP_MESSAGE, AskUser, ask_user_tool
from loom.agent.tools.registry import ToolRegistry
from loom.contracts import Response, ToolCall
from loom.testing.fake_llm import FakeLLM


def events() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    log: list[tuple[str, dict[str, Any]]] = []
    return log, lambda kind, **fields: log.append((kind, fields))


async def test_the_answer_comes_back_as_the_tool_result() -> None:
    """FR-ASK-01."""
    ask = AskUser(prompt_fn=lambda **kw: "postgres")
    assert await ask(question="which database?", options=["sqlite", "postgres"]) == "postgres"


async def test_the_question_and_the_answer_are_both_events() -> None:
    """FR-ASK-05 — a resumed run has to be able to reuse the answer rather than re-ask."""
    log, on_event = events()
    ask = AskUser(prompt_fn=lambda **kw: "sqlite", on_event=on_event, phase="design")
    await ask(question="which database?", options=["sqlite"], default="sqlite")

    assert [k for k, _ in log] == ["ask_user", "user_answer"]
    assert log[0][1] == {
        "question": "which database?",
        "options": ["sqlite"],
        "default": "sqlite",
        "phase": "design",
    }
    assert log[1][1] == {"answer": "sqlite", "assumed": False, "phase": "design"}


async def test_the_fourth_question_in_a_phase_is_capped() -> None:
    """FR-ASK-02 — three is a conversation, four is a survey."""
    ask = AskUser(prompt_fn=lambda **kw: "yes")
    for _ in range(CAP):
        assert await ask(question="?") == "yes"
    assert await ask(question="?") == CAP_MESSAGE
    assert await ask(question="?") == CAP_MESSAGE


async def test_a_capped_question_never_reaches_the_user() -> None:
    seen: list[str] = []

    def prompt(**kw: Any) -> str:
        seen.append(str(kw["question"]))
        return "yes"

    ask = AskUser(prompt_fn=prompt)
    for i in range(CAP + 2):
        await ask(question=f"q{i}")
    assert seen == ["q0", "q1", "q2"]


async def test_the_cap_is_per_phase() -> None:
    ask = AskUser(prompt_fn=lambda **kw: "yes", phase="plan")
    for _ in range(CAP):
        await ask(question="?")
    assert await ask(question="?") == CAP_MESSAGE

    ask.start_phase("design")
    assert await ask(question="?") == "yes"


async def test_unattended_mode_takes_the_declared_default_without_prompting() -> None:
    """FR-ASK-03."""
    log, on_event = events()
    ask = AskUser(
        prompt_fn=lambda **kw: pytest.fail("--yes prompted the user"), yes=True, on_event=on_event
    )
    assert await ask(question="which database?", default="sqlite") == "sqlite"
    assert log[1] == ("user_answer", {"answer": "sqlite", "assumed": True, "phase": ""})


async def test_unattended_mode_with_no_default_says_to_assume_and_carry_on() -> None:
    ask = AskUser(yes=True)
    answer = await ask(question="which database?")
    assert "assumption" in answer


async def test_no_prompt_function_behaves_like_unattended() -> None:
    """A piped run has no terminal to ask through; that is not an error, it is a default."""
    ask = AskUser(default_answer=None)
    assert await ask(question="?", default="sqlite") == "sqlite"


async def test_an_async_prompt_function_is_awaited() -> None:
    async def prompt(**kw: Any) -> str:
        return "async answer"

    assert await AskUser(prompt_fn=prompt)(question="?") == "async answer"


async def test_unexpected_arguments_from_the_model_are_ignored() -> None:
    ask = AskUser(prompt_fn=lambda **kw: "ok")
    assert await ask(question="?", suggestions=["a"], reasoning="because") == "ok"


# --------------------------------------------------------------------------- the tool spec


def test_the_tool_advertises_question_options_and_default() -> None:
    props = ask_user_tool().spec()["function"]["parameters"]["properties"]
    assert set(props) == {"question", "options", "default"}
    assert ask_user_tool().name == "ask_user"


async def test_the_registered_handler_refuses_to_run() -> None:
    """FR-ASK-04 — if this ever executes, the loop stopped routing and nobody noticed."""
    with pytest.raises(RuntimeError, match="ask_user_fn"):
        await ToolRegistry([ask_user_tool()]).execute("ask_user", {"question": "?"})


async def test_end_to_end_the_loop_routes_the_model_to_the_user_and_back() -> None:
    """FR-ASK-01 + FR-ASK-04 through the real loop, with the real tool registered."""
    log, on_event = events()
    ask = AskUser(prompt_fn=lambda **kw: "sqlite", on_event=on_event, phase="design")
    fake = FakeLLM(
        [
            Response(
                tool_calls=[ToolCall(id="c1", name="ask_user", arguments={"question": "db?"})]
            ),
            Response(text="using sqlite"),
        ]
    )
    result = await run_agent_loop(
        provider=fake,
        system="s",
        task="t",
        tools=ToolRegistry([ask_user_tool()]),
        max_turns=5,
        max_usd=1.0,
        ask_user_fn=ask,
        on_event=on_event,
    )
    assert result.status == "passed"
    assert next(m for m in result.messages if m["role"] == "tool")["content"] == "sqlite"
    assert [k for k, _ in log] == [
        "turn",
        "tool_call",
        "ask_user",
        "user_answer",
        "tool_result",
        "turn",
    ]
