"""The session shell: what `loom` with no subcommand does before the REPL takes over.

Banner, the first-run initialise offer (FR-CLI-05), and the no-TTY rule (FR-CLI-01). The REPL
itself is WP-8.2, injected as `repl`; until then a session is the banner and nothing more.
Presentation only — the state is handed in already resolved, and initialising is an injected
action, so this module holds no business logic (SRS §2.5).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import TextIO

from loom.tui.anim import animating
from loom.tui.banner import BannerState, render_banner

#: What a plain Enter (or piped EOF) means at the initialise prompt: no.
_YES = {"y", "yes"}


def _reveal(lines: Sequence[str], out: TextIO, *, delay: float) -> None:
    """Design §03 — stagger the header a line at a time so identity lands first and the eye ends
    on the prompt. FR-ANIM-04: this only reorders *when* the same lines appear; the still prints
    them at once, so a piped or dumb-terminal run loses nothing."""
    for line in lines:
        out.write(line + "\n")
        out.flush()
        time.sleep(delay)


def start_session(
    *,
    state: BannerState,
    initialised: bool,
    is_tty: bool,
    out: TextIO,
    ask: Callable[[str], str],
    on_init: Callable[[], None],
    repl: Callable[[BannerState], int] | None = None,
) -> int:
    """Run a session and return its exit code (0 unless the injected REPL says otherwise).

    - No `.loom/`: offer to initialise (FR-CLI-05). Declining — or no terminal to ask at —
      writes an explanation and exits 0 rather than failing.
    - No TTY: render the banner (useful piped) but never start the REPL (FR-CLI-01).
    """
    if not initialised:
        if not is_tty:
            out.write("no .loom/ here — run `loom init` to set one up.\n")
            return 0
        if ask(f"Initialise Loom in {state.cwd}? [y/N] ").strip().lower() in _YES:
            on_init()
        else:
            out.write("not initialising — nothing was written.\n")
            return 0

    # The live REPL renders the prompt, the rule and the mode hint itself (design §03), so the
    # interactive banner is the header only; the still keeps the full frame.
    live = is_tty and repl is not None
    banner = render_banner(state, include_input=not live)
    if live and animating(state.cap):
        _reveal(banner.splitlines(), out, delay=1.0 / max(1, state.theme.max_fps))
    else:
        out.write(banner + "\n")
    if live and repl is not None:  # `repl is not None` re-narrows for the type checker
        return repl(state)
    return 0
