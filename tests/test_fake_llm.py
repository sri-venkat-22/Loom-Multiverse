from __future__ import annotations

import pytest

from loom.contracts import Provider, Response, ToolCall
from loom.testing.fake_llm import FakeLLM, ScriptExhausted


def _tool(name: str, **args: object) -> Response:
    return Response(tool_calls=[ToolCall(id=f"c_{name}", name=name, arguments=args)])


async def test_three_turn_tool_sequence_replays_in_order() -> None:
    fake = FakeLLM(
        [
            _tool("read_file", path="a.py"),
            _tool("write_file", path="a.py", content="x"),
            _tool("run_bash", command="pytest -q"),
            Response(text="done", usd_cost=0.01),
        ]
    )
    names = []
    for _ in range(3):
        r = await fake.complete([{"role": "user", "content": "go"}])
        names.append(r.tool_calls[0].name)
    final = await fake.complete([{"role": "user", "content": "go"}])

    assert names == ["read_file", "write_file", "run_bash"]
    assert final.text == "done" and final.tool_calls == []
    assert fake.call_count == 4 and fake.remaining == 0


async def test_it_is_a_provider() -> None:
    assert isinstance(FakeLLM([]), Provider)


async def test_calls_record_the_messages_and_are_snapshots() -> None:
    fake = FakeLLM([Response(text="one"), Response(text="two")])
    messages = [{"role": "user", "content": "first"}]
    await fake.complete(messages)
    messages.append({"role": "assistant", "content": "one"})
    await fake.complete(messages)

    assert [len(c) for c in fake.calls] == [1, 2]
    assert fake.calls[0] == [{"role": "user", "content": "first"}]
    messages[0]["content"] = "mutated after the fact"
    assert fake.calls[0][0]["content"] == "first"


async def test_running_dry_raises_and_still_records_the_call() -> None:
    fake = FakeLLM([Response(text="only one")])
    await fake.complete([{"role": "user", "content": "a"}])
    with pytest.raises(ScriptExhausted, match="only 1 response"):
        await fake.complete([{"role": "user", "content": "b"}])
    assert fake.call_count == 2


async def test_tool_specs_are_recorded_for_absence_assertions() -> None:
    fake = FakeLLM([Response(text="x"), Response(text="y")])
    specs = [{"type": "function", "function": {"name": "web_search"}}]
    await fake.complete([], tools=specs)
    await fake.complete([], tools=None)

    assert fake.tool_names_seen(0) == ["web_search"]
    assert "read_file" not in fake.tool_names_seen(0)
    assert fake.tool_names_seen(1) == []
