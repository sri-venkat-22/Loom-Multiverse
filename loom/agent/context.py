"""Transcript compaction. WP-5.2 — NFR-REL, the long-build survival property.

A build that runs forty turns of read-edit-test accumulates a transcript far larger than any
provider's context window — mostly tool output: file reads, test logs, scaffold dumps. This
shrinks the old tool output while keeping the recent turns verbatim, so the model still sees the
last dozen turns in full and the history before that as a gist.

The one invariant that makes this safe is the one the blueprint addendum (bug #4) calls out:
an assistant `tool_calls` entry whose matching `tool` result has been dropped is a hard 400 on
most providers. So compaction **never drops a message** — it only shortens the `content` of old
`tool` messages. Every `tool_call_id` therefore still has its answer, structurally, and
`tests/test_context.py` asserts exactly that on the output.

A "turn" is one assistant message plus the `tool`/`user` messages that follow it before the next
assistant message. Compaction operates on those whole groups, never on individual messages —
which is the other half of why a call and its result never come apart.

ponytail: local, deterministic summary (gist + elision count), no model call. If token pressure
ever needs the transcript smaller still, the upgrade is to drop whole old *groups* (never lone
messages) once their gist has been folded into a running summary — same invariant, more work.
"""

from __future__ import annotations

from typing import Any

Message = dict[str, Any]

#: Turns kept verbatim at the tail. Twelve is a couple of full edit-test-grade cycles — enough
#: for the model to see what it just did without re-reading the whole build.
KEEP_TURNS = 12

#: Prefix stamped onto a shortened tool output. Also the idempotency latch: a second pass sees it
#: and leaves the message alone, so `compact(compact(x)) == compact(x)`.
ELIDED = "[compacted]"

#: How much of an old tool output survives as a gist before the elision note. Enough to keep the
#: first error line or the head of a file, not enough to keep paying for the whole thing.
GIST_CHARS = 200


def compact(messages: list[Message], *, keep_turns: int = KEEP_TURNS) -> list[Message]:
    """Return a compacted copy of `messages`: last `keep_turns` turns verbatim, older tool
    outputs shrunk to a gist. Idempotent, and never orphans a `tool` message from its call.

    The input is never mutated — every message in the result is a fresh dict.
    """
    preamble, groups = _group(messages)
    if len(groups) <= keep_turns:
        # Nothing old enough to shrink. Still return copies so callers cannot alias our input.
        return [dict(m) for m in messages]

    cutoff = len(groups) - keep_turns
    out: list[Message] = [dict(m) for m in preamble]
    for index, group in enumerate(groups):
        if index < cutoff:
            out.extend(_summarize(m) for m in group)
        else:
            out.extend(dict(m) for m in group)
    return out


def _group(messages: list[Message]) -> tuple[list[Message], list[list[Message]]]:
    """Split into (preamble, turns). Preamble is the leading system/user run before any
    assistant message; each turn is an assistant message and everything up to the next one."""
    index = 0
    while index < len(messages) and messages[index].get("role") != "assistant":
        index += 1
    preamble = messages[:index]

    groups: list[list[Message]] = []
    for message in messages[index:]:
        if message.get("role") == "assistant":
            groups.append([message])
        else:
            # Unreachable unless the transcript starts a non-assistant run mid-stream; the
            # `while` above guarantees the first post-preamble message opens a group.
            groups[-1].append(message)
    return preamble, groups


def _summarize(message: Message) -> Message:
    """Shorten a `tool` message's content to a gist. Any other message is copied unchanged —
    assistant `tool_calls` entries in particular must survive intact, or the pairing breaks."""
    if message.get("role") != "tool":
        return dict(message)
    content = message.get("content", "")
    if not isinstance(content, str) or content.startswith(ELIDED) or len(content) <= GIST_CHARS:
        return dict(message)  # already small or already compacted — the idempotent path
    gist = content[:GIST_CHARS].rstrip()
    elided = len(content) - len(gist)
    return {**message, "content": f"{ELIDED} {gist} … ({elided} chars elided)"}
