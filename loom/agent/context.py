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

import json
from pathlib import Path
from typing import Any

Message = dict[str, Any]

#: Turns kept verbatim at the tail. Twelve is a couple of full edit-test-grade cycles — enough
#: for the model to see what it just did without re-reading the whole build.
KEEP_TURNS = 12

#: Rough chars-per-token, for reporting reclaimed *tokens* off a reclaimed *char* count. A
#: heuristic, not a tokenizer — good enough for "you saved about N tokens", never used for billing.
CHARS_PER_TOKEN = 4

#: The loop auto-compacts once the serialised transcript passes this. ~50k tokens: high enough that
#: only a genuinely long build trips it (so normal runs never pay for a needless pass), low enough
#: to stay clear of every provider's context window. ponytail: a serialised-char proxy for tokens;
#: swap in a real token count here if a model's window ever gets close before this fires.
COMPACT_ABOVE_CHARS = 200_000

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


def transcript_chars(messages: list[Message]) -> int:
    """The transcript's size the way a provider sees it — serialised, so tool_call arguments and
    every field count, not just visible `content`."""
    return len(json.dumps(messages, default=str))


def compaction_report(
    messages: list[Message], *, keep_turns: int = KEEP_TURNS
) -> tuple[list[Message], int, str]:
    """Compact `messages` and describe the saving: `(compacted, reclaimed_chars, one_line)`.

    `reclaimed_chars` is 0 when there was nothing old enough to shrink, and the line says so —
    the two callers (the loop's auto-pass and `/compact`) both branch on that.
    """
    before = transcript_chars(messages)
    out = compact(messages, keep_turns=keep_turns)
    after = transcript_chars(out)
    reclaimed = max(0, before - after)
    if reclaimed == 0:
        return out, 0, "nothing to compact — the context is already within the keep-window"
    tokens = reclaimed // CHARS_PER_TOKEN
    line = (
        f"compacted the context: reclaimed ~{tokens:,} tokens "
        f"({before:,} → {after:,} chars), last {keep_turns} turns kept verbatim"
    )
    return out, reclaimed, line


def compact_run(root: Path, run_id: str) -> str:
    """`/compact` on demand (FR-SESS-06): compact a run's persisted build transcript in place and
    report the tokens reclaimed. Business logic in core, so the REPL stays presentation-only.

    The build writes `runs/<id>/transcript.json`; there is no live in-REPL context to compact in
    R1 (a build runs synchronously), so on-demand compaction targets that file. A missing or torn
    file is a message, never a crash.
    """
    from loom.agent.tools.fs import atomic_write
    from loom.session import runs_dir

    path = runs_dir(Path(root)) / run_id / "transcript.json" if run_id else None
    if path is None or not path.is_file():
        return "no build transcript to compact yet — /compact works once a build has run"
    try:
        messages = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "the build transcript could not be read"
    if not isinstance(messages, list):
        return "the build transcript is not in the expected shape"
    out, reclaimed, line = compaction_report(messages)
    if reclaimed > 0:
        atomic_write(path, json.dumps(out) + "\n")
    return line
