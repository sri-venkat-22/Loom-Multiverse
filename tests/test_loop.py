"""FR-AGENT-01…06 — the tool-calling loop, entirely against FakeLLM.

The seven cases in build plan §9.1, plus the accounting the pipeline depends on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from loom.agent.loop import LoopResult, run_agent_loop
from loom.agent.tools.registry import ToolRegistry, tool
from loom.contracts import Response, ToolCall
from loom.testing.fake_llm import FakeLLM

# --------------------------------------------------------------------------- fixtures


@tool
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"


@tool
def explode() -> str:
    """Always raises."""
    raise RuntimeError("the tool broke")


@tool
def ask_user(question: str) -> str:
    """Ask the user. The loop must never dispatch this here."""
    raise AssertionError("ask_user reached tools.execute — FR-ASK-04 violated")


def registry() -> ToolRegistry:
    return ToolRegistry([echo, explode, ask_user])


def call(name: str, cid: str = "c1", **args: Any) -> Response:
    return Response(tool_calls=[ToolCall(id=cid, name=name, arguments=args)], usd_cost=0.01)


class FakeScore:
    def __init__(self, total: float) -> None:
        self.total = total
        self.criteria: list[Any] = []

    def as_feedback(self) -> str:
        return f"score {self.total}: fix the failing criteria"


class FakeRubric:
    """Whatever WP-4.2 lands, the loop only ever needs `.threshold` and `await .grade()`."""

    def __init__(self, totals: list[float], threshold: float = 0.85) -> None:
        self.totals = list(totals)
        self.threshold = threshold
        self.grades = 0

    async def grade(self) -> FakeScore:
        total = self.totals[min(self.grades, len(self.totals) - 1)]
        self.grades += 1
        return FakeScore(total)


async def run(script: list[Response], **kwargs: Any) -> tuple[LoopResult, FakeLLM]:
    fake = FakeLLM(script)
    result = await run_agent_loop(
        provider=fake,
        system="you build software",
        task="build it",
        tools=kwargs.pop("tools", registry()),
        max_turns=kwargs.pop("max_turns", 10),
        max_usd=kwargs.pop("max_usd", 1.0),
        **kwargs,
    )
    return result, fake


# --------------------------------------------------------------------------- 1


async def test_a_tool_sequence_then_text_passes_and_pairs_every_tool_call() -> None:
    """FR-AGENT-03 — exactly one tool message per call, in order, with the matching id."""
    both = Response(
        tool_calls=[
            ToolCall(id="a1", name="echo", arguments={"text": "one"}),
            ToolCall(id="a2", name="echo", arguments={"text": "two"}),
        ],
        usd_cost=0.01,
    )
    result, fake = await run([call("echo", "c1", text="first"), both, Response(text="done")])

    assert result.status == "passed"
    assert result.turns_used == 3
    assert fake.call_count == 3

    kinds = [m["role"] for m in result.messages]
    assert kinds == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    tool_messages = [m for m in result.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["c1", "a1", "a2"]
    assert [m["content"] for m in tool_messages] == ["echo: first", "echo: one", "echo: two"]

    # Every tool message has an assistant tool_call that produced it (the FR-AGENT-10 invariant,
    # established here so compaction has something true to preserve).
    emitted = {
        tc["id"]
        for m in result.messages
        if m["role"] == "assistant"
        for tc in m.get("tool_calls", [])
    }
    assert {m["tool_call_id"] for m in tool_messages} <= emitted


async def test_tool_arguments_reach_the_transcript_as_json() -> None:
    result, _ = await run([call("echo", "c1", text="hi"), Response(text="ok")])
    assistant = next(m for m in result.messages if m["role"] == "assistant")
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"text": "hi"}
    assert assistant["tool_calls"][0]["function"]["name"] == "echo"


# --------------------------------------------------------------------------- 2


async def test_the_budget_is_checked_before_the_next_call_not_after() -> None:
    """FR-AGENT-02 — the abort must never be charged for a call it will not use."""
    result, fake = await run(
        [
            Response(
                tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})], usd_cost=0.60
            ),
            Response(text="never reached", usd_cost=99.0),
        ],
        max_usd=0.50,
    )
    assert result.status == "budget_exhausted"
    assert fake.call_count == 1, "the loop paid for a call it had already decided not to use"
    assert result.usd_spent == pytest.approx(0.60)
    assert fake.remaining == 1


async def test_exhausting_max_turns_is_also_budget_exhausted() -> None:
    result, fake = await run([call("echo", f"c{i}", text="x") for i in range(5)], max_turns=3)
    assert result.status == "budget_exhausted"
    assert result.turns_used == 3
    assert fake.call_count == 3


async def test_usd_is_accumulated_across_turns() -> None:
    result, _ = await run([call("echo", "c1", text="x"), Response(text="done", usd_cost=0.02)])
    assert result.usd_spent == pytest.approx(0.03)


# --------------------------------------------------------------------------- 3


async def test_a_tool_that_raises_becomes_a_tool_message_and_the_loop_continues() -> None:
    """FR-AGENT-04 — it MUST NOT propagate."""
    result, _ = await run([call("explode", "c1"), Response(text="recovered")])
    assert result.status == "passed"
    message = next(m for m in result.messages if m["role"] == "tool")
    assert "the tool broke" in message["content"]
    assert message["tool_call_id"] == "c1"


async def test_a_denied_command_is_framed_not_fatal(tmp_path: Path) -> None:
    """SEC-02's refusal arrives at the model as text it can act on, like any other tool error."""
    from loom.agent.tools.bash import bash_tool

    tools = ToolRegistry([bash_tool(tmp_path)])
    result, _ = await run(
        [call("run_bash", "c1", command="git push origin main"), Response(text="ok, I won't")],
        tools=tools,
    )
    assert result.status == "passed"
    assert "git push" in next(m for m in result.messages if m["role"] == "tool")["content"]


# --------------------------------------------------------------------------- 4


async def test_an_unknown_tool_name_lists_the_available_tools() -> None:
    """FR-AGENT-05 — never a KeyError."""
    result, _ = await run([call("delete_everything", "c1"), Response(text="fine")])
    assert result.status == "passed"
    message = next(m for m in result.messages if m["role"] == "tool")
    assert "delete_everything" in message["content"]
    assert "echo" in message["content"] and "explode" in message["content"]


# --------------------------------------------------------------------------- 5


async def test_a_score_below_threshold_appends_feedback_and_continues() -> None:
    rubric = FakeRubric([0.40, 0.95])
    result, fake = await run(
        [Response(text="v1"), Response(text="v2")],
        rubric=rubric,
    )
    assert result.status == "passed"
    assert rubric.grades == 2
    assert fake.call_count == 2
    feedback = [m for m in result.messages if m["role"] == "user"][-1]
    assert "score 0.4" in feedback["content"]
    assert result.score is not None and result.score.total == pytest.approx(0.95)


async def test_a_score_at_the_threshold_passes_immediately() -> None:
    result, fake = await run([Response(text="v1")], rubric=FakeRubric([0.85]))
    assert result.status == "passed" and fake.call_count == 1


async def test_without_a_rubric_the_first_text_response_is_the_end() -> None:
    result, fake = await run([Response(text="done"), Response(text="unused")])
    assert result.status == "passed" and fake.call_count == 1 and result.score is None


# --------------------------------------------------------------------------- 6


async def test_three_non_improving_rounds_stall_before_the_turn_cap() -> None:
    """Do not burn the budget re-proving the model cannot fix it."""
    rubric = FakeRubric([0.50, 0.50, 0.505, 0.50])
    result, fake = await run([Response(text=f"v{i}") for i in range(9)], rubric=rubric, max_turns=9)

    assert result.status == "stalled"
    assert fake.call_count == 4, "it kept paying after the third flat round"
    assert result.score is not None and result.score.total == pytest.approx(0.505)


async def test_steady_improvement_is_not_a_stall() -> None:
    rubric = FakeRubric([0.10, 0.30, 0.50, 0.70, 0.90])
    result, _ = await run([Response(text=f"v{i}") for i in range(5)], rubric=rubric, max_turns=9)
    assert result.status == "passed"


# --------------------------------------------------------------------------- 7


async def test_ask_user_routes_to_the_callback_never_to_the_registry() -> None:
    """FR-ASK-04 — the registry's ask_user handler asserts if it is ever reached."""
    asked: list[dict[str, Any]] = []

    async def ask(**kwargs: Any) -> str:
        asked.append(kwargs)
        return "sqlite, please"

    result, _ = await run(
        [call("ask_user", "c1", question="which database?"), Response(text="sqlite it is")],
        ask_user_fn=ask,
    )
    assert result.status == "passed"
    assert asked == [{"question": "which database?"}]
    assert next(m for m in result.messages if m["role"] == "tool")["content"] == "sqlite, please"


async def test_ask_user_without_a_callback_says_so_rather_than_crashing() -> None:
    result, _ = await run([call("ask_user", "c1", question="?"), Response(text="assumed")])
    assert result.status == "passed"
    assert "no way to ask" in next(m for m in result.messages if m["role"] == "tool")["content"]


# --------------------------------------------------------------------------- events, blocked


async def test_every_turn_and_tool_call_is_an_event() -> None:
    """NFR-OBS-01 — anything the terminal will show is already an event."""
    events: list[tuple[str, dict[str, Any]]] = []
    await run(
        [call("echo", "c1", text="x"), Response(text="done")],
        on_event=lambda kind, **fields: events.append((kind, fields)),
    )
    kinds = [k for k, _ in events]
    assert kinds == ["turn", "tool_call", "tool_result", "turn"]
    assert events[1][1]["name"] == "echo"
    assert events[0][1]["turn"] == 1


async def test_a_response_with_neither_text_nor_tools_is_blocked() -> None:
    """FR-AGENT-01's fourth status. Re-prompting an empty response just buys another one."""
    result, fake = await run([Response(), Response(text="unused")])
    assert result.status == "blocked" and fake.call_count == 1


async def test_the_tool_specs_are_offered_on_every_call() -> None:
    _, fake = await run([Response(text="done")])
    assert fake.tool_names_seen(0) == ["echo", "explode", "ask_user"]


# --------------------------------------------------------------------------- FR-AGENT-06


def test_the_loop_knows_nothing_about_any_provider() -> None:
    """FR-AGENT-06 — no SDK import, and above all no `caching=True`, which in an agent loop
    replays one tool call forever.

    Checked on the parsed source rather than by grep, so the docstring may explain the trap
    without tripping the guard against it.
    """
    import ast

    tree = ast.parse((Path(__file__).resolve().parent.parent / "loom/agent/loop.py").read_text())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported & {"litellm", "anthropic", "openai"} == set()

    keywords = {
        kw.arg for node in ast.walk(tree) if isinstance(node, ast.Call) for kw in node.keywords
    }
    assert "caching" not in keywords, "loop.py passes a litellm-specific kwarg"


async def test_a_secret_in_a_tool_argument_never_reaches_the_event_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-06, NFR-OBS-03 — tool arguments are the one part of a call that becomes an event."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-planted-fake-key")
    events: list[tuple[str, dict[str, Any]]] = []
    await run(
        [call("echo", "c1", text="the key is sk-ant-planted-fake-key"), Response(text="done")],
        on_event=lambda kind, **fields: events.append((kind, fields)),
    )
    logged = [f for k, f in events if k == "tool_call"][0]
    assert "sk-ant-planted-fake-key" not in str(logged)
    assert "[redacted]" in logged["args"]["text"]
