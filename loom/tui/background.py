"""`/background` — run a phase off the main thread so the prompt comes straight back.

FR-SESS-07 (MAY, R1.1). The backgrounded run keeps writing its JSONL events through its own
`Session`, so it stays observable through `loom status` and the REPL's `/status` while it works.
That observability, not a job-control UI, is what the requirement asks for.

ponytail: one run at a time, a daemon thread, no cancellation. A backgrounded run cannot be paused
or pulled back to the foreground, and it dies with the process. The richer version — detaching an
*already-running* foreground phase on a keypress and re-attaching to it — needs the async REPL that
WP-8.5 was deferred behind (§6.8). This is the half that fits today's synchronous REPL, and it is
the half that actually frees the terminal.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class BackgroundRun:
    """A phase pipeline running in a daemon thread. `alive` is what the REPL's run state reads to
    refuse mutating commands while it works, and what `/background` checks before starting a
    second one."""

    def __init__(self, label: str, target: Callable[[], None]) -> None:
        self.label = label
        self._thread = threading.Thread(target=target, name=f"loom-bg-{label}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()
