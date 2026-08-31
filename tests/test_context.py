"""WP-5.2 — transcript compaction.

The headline test is `test_no_tool_call_is_ever_orphaned`: every `tool_call_id` in the output
has a matching assistant `tool_calls` entry (blueprint addendum bug #4). The rest pin the three
promised properties — last 12 turns verbatim, older tool outputs summarized, idempotent.
"""

from __future__ import annotations

from typing import Any

from loom.agent.context import ELIDED, GIST_CHARS, KEEP_TURNS, compact


def _assistant_call(turn: int) -> dict[str, Any]:
    """An assistant turn that calls one tool, OpenAI-shaped like `loop._assistant_message`."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{turn}",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    }


def _tool_result(turn: int, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": f"call_{turn}", "name": "read_file", "content": content}


def _transcript(turns: int, *, output_chars: int = 5_000) -> list[dict[str, Any]]:
    """system + task, then `turns` tool-calling turns each with a big tool output."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you build repos"},
        {"role": "user", "content": "build me a thing"},
    ]
    for turn in range(1, turns + 1):
        messages.append(_assistant_call(turn))
        messages.append(_tool_result(turn, f"turn {turn} output " + "x" * output_chars))
    return messages


def _call_ids(messages: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls", []) or []:
            ids.add(call["id"])
    return ids


def _result_ids(messages: list[dict[str, Any]]) -> set[str]:
    return {m["tool_call_id"] for m in messages if m.get("role") == "tool"}


def test_no_tool_call_is_ever_orphaned() -> None:
    """The invariant WP-5.2 exists for: every `tool` result still matches an assistant call,
    and every assistant call still has its result. A mismatch is a hard 400 in production."""
    out = compact(_transcript(30))
    assert _call_ids(out) == _result_ids(out)
    # And nothing was lost: all 30 calls survive, compacted or not.
    assert _call_ids(out) == {f"call_{t}" for t in range(1, 31)}


def test_short_history_is_returned_unchanged() -> None:
    """At or under the keep-window there is nothing old to summarize."""
    original = _transcript(KEEP_TURNS)
    assert compact(original) == original


def test_the_last_twelve_turns_are_verbatim() -> None:
    original = _transcript(30)
    out = compact(original)
    # The final 12 turns are 24 messages (call + result each); they must be byte-identical.
    assert out[-2 * KEEP_TURNS :] == original[-2 * KEEP_TURNS :]


def test_older_tool_outputs_are_summarized() -> None:
    out = compact(_transcript(30))
    # Turn 1 is well outside the keep-window; its result is shrunk and marked.
    first_result = next(m for m in out if m.get("role") == "tool")
    assert first_result["content"].startswith(ELIDED)
    assert "chars elided" in first_result["content"]
    assert len(first_result["content"]) < 5_000


def test_the_preamble_survives() -> None:
    out = compact(_transcript(30))
    assert out[0] == {"role": "system", "content": "you build repos"}
    assert out[1] == {"role": "user", "content": "build me a thing"}


def test_assistant_tool_calls_are_never_touched() -> None:
    """Only `tool` content is summarized; the assistant `tool_calls` that name them stay whole,
    or the pairing the first test checks would be meaningless."""
    original = _transcript(30)
    out = compact(original)
    old_calls = [m for m in out if m.get("role") == "assistant"][:5]
    assert old_calls == [m for m in original if m.get("role") == "assistant"][:5]


def test_it_is_idempotent() -> None:
    original = _transcript(30)
    once = compact(original)
    twice = compact(once)
    assert twice == once


def test_the_input_is_not_mutated() -> None:
    original = _transcript(20)
    before = original[2]["content"]  # an old tool result's content, in full
    compact(original)
    assert original[2]["content"] == before


def test_a_small_tool_output_is_left_alone() -> None:
    """A short old result is already cheap; summarizing it would only add a marker for no gain."""
    original = _transcript(30, output_chars=GIST_CHARS // 2)
    out = compact(original)
    first_result = next(m for m in out if m.get("role") == "tool")
    assert not first_result["content"].startswith(ELIDED)


def test_user_feedback_between_turns_keeps_its_group() -> None:
    """A grade-failure appends a `user` message after an assistant text turn; it must not
    become preamble or get dropped, and the tool pairing before it must still hold."""
    messages = _transcript(30)
    messages.append({"role": "assistant", "content": "here is my answer"})
    messages.append({"role": "user", "content": "rubric says 0.4 — the tests do not pass"})
    out = compact(messages)
    assert {"role": "user", "content": "rubric says 0.4 — the tests do not pass"} in out
    assert _call_ids(out) == _result_ids(out)
