"""Non-TTY line output for pipes and CI. WP-5.5 — FR-HEADLESS-02/03.

The interactive terminal (WP-8.x) is a progressive enhancement. When stdout is not a TTY there
is no live view and no colour; this renders the same event stream as plain text instead:

* one line per turn — phase, turn, tool, elapsed, running USD — with **no escape sequence and no
  cursor movement** (FR-HEADLESS-02), so it reads correctly in a pipe or a CI log; or
* `--json` — the event stream verbatim as JSON Lines, human output suppressed, for scripting
  (FR-HEADLESS-03).

Both are sinks over the same events `session.log_event` already writes, so a piped run and a TTY
run log identically and only the rendering differs (FR-ANIM-04). Neither imports a terminal
library — this module is line output, nothing more.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, TextIO

Event = dict[str, Any]
Sink = Callable[[Event], None]


class TurnLine:
    """FR-HEADLESS-02 — one plain line per turn. Tracks the current phase from `phase_started`
    and elapsed from the first turn, because a `turn` event carries neither."""

    def __init__(self, out: TextIO, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.out = out
        self.clock = clock
        self.phase = "?"
        self.start: float | None = None

    def __call__(self, event: Event) -> None:
        kind = event.get("kind")
        if kind == "phase_started":
            self.phase = str(event.get("phase", "?"))
        elif kind == "turn":
            if self.start is None:
                self.start = self.clock()
            elapsed = self.clock() - self.start
            self.out.write(
                f"{self.phase:<8} turn {int(event.get('turn', 0)):>2}  "
                f"{_tool(event):<24} {elapsed:6.1f}s  ${float(event.get('usd', 0.0)):.4f}\n"
            )
            self.out.flush()


class JsonLine:
    """FR-HEADLESS-03 — every event as one JSON object on its own line. `default=str` so a
    stray non-serialisable field (a Path) degrades to text instead of raising mid-run."""

    def __init__(self, out: TextIO) -> None:
        self.out = out

    def __call__(self, event: Event) -> None:
        self.out.write(json.dumps(event, default=str) + "\n")
        self.out.flush()


class TokenStream:
    """FR-REPL-05 — assistant text rendered as it streams in, not only when the turn is done.

    The provider (`LiteLLMProvider`, when given this as its `on_token`) calls it with each text
    delta as it arrives off the wire. It writes the delta straight through and flushes: the tokens
    *are* the output, so there is no escape sequence and no cursor move here. It also keeps the
    accumulated `text`, which is what the formatter test pins. Wired only on an interactive TTY;
    a piped run streams nothing and gets one `TurnLine` per turn instead (FR-HEADLESS-02).
    """

    def __init__(self, out: TextIO) -> None:
        self.out = out
        self.text = ""

    def __call__(self, delta: str) -> None:
        if not delta:
            return
        self.text += delta
        self.out.write(delta)
        self.out.flush()


def _tool(event: Event) -> str:
    """What the turn did: the tool(s) it called, else `text` if it answered, else a dash."""
    calls = event.get("tool_calls") or []
    if calls:
        return ",".join(str(c) for c in calls)
    return "text" if event.get("text") else "—"


def event_sink(
    out: TextIO,
    *,
    json_mode: bool,
    tty: bool,
    clock: Callable[[], float] = time.monotonic,
) -> Sink | None:
    """The renderer for this run, or `None` to render nothing.

    `--json` wins everywhere. Otherwise plain per-turn lines only when stdout is not a TTY —
    on a TTY the live view (WP-8.3) renders instead, and doubling it here would be noise.
    """
    if json_mode:
        return JsonLine(out)
    if tty:
        return None
    return TurnLine(out, clock=clock)
