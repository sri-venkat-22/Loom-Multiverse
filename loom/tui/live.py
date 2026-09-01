"""The live working indicator — FR-ANIM-01, FR-COST-04, FR-REPL-05, FR-ANIM-06.

Any operation over 500 ms gets an indicator: a spinner, what it is doing, elapsed seconds and the
running dollar total, repainting no more than the theme's `max_fps` (15). It draws only on an
animating TTY; piped or on a dumb terminal the per-turn plain lines of `loom/ui.py` render the
same fields instead, so a captured run and a watched one carry identical events and differ only in
the drawing (FR-ANIM-04). On every exit — return, Ctrl-C or a raised exception — it clears its line
and shows the cursor again (FR-ANIM-06); the escape sequences never outlive the widget.

Presentation only: the caller feeds it elapsed time and the ledger's running total; it owns no
clock of truth and no spend of its own.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from types import TracebackType
from typing import TextIO

from loom.tui.anim import Capability, animating, spinner_frame
from loom.tui.theme import DEFAULT_THEME, Theme

#: An operation shorter than this never shows an indicator — a flash of spinner on a 200 ms call
#: is noise, and FR-ANIM-01 draws the line at half a second.
SHOW_AFTER_S = 0.5

_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"
_CLEAR_LINE = "\r\x1b[K"


def working_line(
    theme: Theme, *, activity: str, elapsed: float, usd: float, cap: Capability
) -> str:
    """The indicator's text — spinner, activity, elapsed, running cost. Plain text with no escape
    byte, so the same formatter feeds a pipe; the fields match `ui.TurnLine` so the two agree."""
    frame = spinner_frame(theme, elapsed=elapsed, cap=cap)
    lead = f"{frame} " if frame else ""
    return f"{lead}{activity:<28} {elapsed:6.1f}s  ${usd:.4f}"


class WorkingIndicator:
    """Repaints one in-place line while a phase works. Construct it around a run, `start` each
    operation, `tick` it as cost and activity change; use it as a context manager so the terminal
    is always restored:

        with WorkingIndicator(out, theme, cap=cap) as ind:
            ind.start("validating")
            ...  ind.tick(usd=spent)

    On a non-animating capability every method is a no-op — the pipe renderer owns that case.
    """

    def __init__(
        self,
        out: TextIO,
        theme: Theme = DEFAULT_THEME,
        *,
        cap: Capability = "truecolor",
        no_animation: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.out = out
        self.theme = theme
        self.cap = cap
        self.clock = clock
        self.on = animating(cap, no_animation=no_animation)
        self._min_repaint = 1.0 / max(1, theme.max_fps)  # FR-ANIM-01 — repaint cap
        self._activity = ""
        self._started: float | None = None
        self._last_paint = 0.0
        self._drawn = False

    def __enter__(self) -> WorkingIndicator:
        if self.on:
            self.out.write(_HIDE_CURSOR)
            self.out.flush()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.clear()
        if self.on:
            self.out.write(_SHOW_CURSOR)
            self.out.flush()

    def start(self, activity: str) -> None:
        """Begin timing a new operation. The indicator stays hidden until `SHOW_AFTER_S`."""
        self._activity = activity
        self._started = self.clock()
        self._last_paint = 0.0

    def tick(self, *, usd: float, activity: str | None = None) -> None:
        """Repaint if the operation has been running past the threshold and the fps window has
        elapsed. Cheap to call every event — it throttles itself."""
        if not self.on or self._started is None:
            return
        if activity is not None:
            self._activity = activity
        now = self.clock()
        elapsed = now - self._started
        if elapsed < SHOW_AFTER_S:
            return
        if self._drawn and now - self._last_paint < self._min_repaint:
            return
        self._last_paint = now
        self._drawn = True
        line = working_line(
            self.theme, activity=self._activity, elapsed=elapsed, usd=usd, cap=self.cap
        )
        self.out.write(_CLEAR_LINE + line)
        self.out.flush()

    def clear(self) -> None:
        """Erase the current line, if one was drawn. Idempotent — safe to call from `stop` and
        again from `__exit__`."""
        if self.on and self._drawn:
            self.out.write(_CLEAR_LINE)
            self.out.flush()
            self._drawn = False
