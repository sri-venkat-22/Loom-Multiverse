"""The session shell: what `loom` with no subcommand does before the REPL takes over.

Banner, the first-run initialise offer (FR-CLI-05), and the no-TTY rule (FR-CLI-01). The REPL
itself is WP-8.2, injected as `repl`; until then a session is the banner and nothing more.
Presentation only — the state is handed in already resolved, and initialising is an injected
action, so this module holds no business logic (SRS §2.5).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TextIO

from loom.tui.banner import BannerState, render_banner

#: What a plain Enter (or piped EOF) means at the initialise prompt: no.
_YES = {"y", "yes"}


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

    out.write(render_banner(state) + "\n")
    if is_tty and repl is not None:
        return repl(state)
    return 0
