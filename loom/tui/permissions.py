"""Strict-mode consent — FR-GATE-05 / SEC-10.

In `strict` mode every file write and every bash command is shown, verbatim, and approved before
it runs. Three answers: **allow once**, **allow for this run**, **deny**. The split that keeps
`loom/tui/` presentation-only (SRS §2.5):

* **`StrictPolicy` is the decision, and it is pure.** It holds the for-this-run grants and, given a
  proposed action, either returns "go" from a standing grant or asks. It imports no terminal library
  and is driven in tests by a scripted `ask`.
* **`ask_permission` is the drawing.** An inline `prompt_toolkit` widget that shows the exact
  command or path and returns the choice. Injected pipe input/output make it drivable with no TTY.

A for-this-run grant is per exact target — approving `write app/main.py` does not silently approve
`write app/db.py` — and lives only in the policy instance, so it cannot outlast the run (the SRS's
one hard rule). The default answer, on Enter or Esc, is **deny**: unattended, the safe choice.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.output import Output

from loom.tui.theme import DEFAULT_THEME, Theme
from loom.tui.widgets import _app, _line

Decision = Literal["once", "run", "deny"]
Kind = Literal["write", "bash"]

#: The modes that gate tool calls. Only `strict` prompts; the others let the phase run (a run's
#: existing budget and jail still apply). One place, so the REPL and the build agent agree.
STRICT_MODES = frozenset({"strict"})


@dataclass(frozen=True)
class Request:
    """One thing about to happen. `target` is the exact path (write) or command line (bash)."""

    kind: Kind
    target: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.target}"

    def prompt(self, theme: Theme = DEFAULT_THEME) -> str:
        verb = "write file" if self.kind == "write" else "run"
        return f"strict mode — {verb}: {self.target}"


@dataclass
class StrictPolicy:
    """Gate for tool calls in strict mode. `check` returns True to proceed, False to deny.

    `ask` is the prompt — `ask_permission` in production, a scripted callable in a test. A `run`
    answer is remembered for the exact target for the life of this instance and nothing longer.
    """

    ask: Callable[[Request], Decision]
    _granted: set[str] = field(default_factory=set)

    def check(self, kind: Kind, target: str) -> bool:
        request = Request(kind, target)
        if request.key in self._granted:  # allowed earlier this run — do not ask twice
            return True
        decision = self.ask(request)
        if decision == "run":
            self._granted.add(request.key)
            return True
        return decision == "once"

    @property
    def granted(self) -> frozenset[str]:
        """The for-this-run grants so far — inspectable, and by construction gone when the policy
        is (FR-GATE-05: a run grant never persists beyond the run)."""
        return frozenset(self._granted)


def should_prompt(mode: str) -> bool:
    return mode in STRICT_MODES


def ask_permission(
    request: Request,
    *,
    theme: Theme = DEFAULT_THEME,
    input: Input | None = None,
    output: Output | None = None,
) -> Decision:
    """The inline strict-mode prompt. `o` allows once, `r` allows for this run, `d`/Esc/Enter deny.
    Deny is the default so an unattended terminal never grants by accident."""
    kb = KeyBindings()

    @kb.add("o")
    @kb.add("O")
    def _once(event: KeyPressEvent) -> None:
        event.app.exit(result="once")

    @kb.add("r")
    @kb.add("R")
    def _run(event: KeyPressEvent) -> None:
        event.app.exit(result="run")

    @kb.add("d")
    @kb.add("D")
    @kb.add("enter")
    @kb.add("escape", eager=True)
    def _deny(event: KeyPressEvent) -> None:
        event.app.exit(result="deny")

    body = HSplit(
        [
            _line(f"  {request.prompt(theme)}"),
            _line("  [o] allow once   [r] allow for this run   [d] deny"),
        ]
    )
    result = _app(Layout(body), kb, output, input).run()
    return result if result in ("once", "run") else "deny"
