"""WP-8.5 — `/background`'s daemon-thread runner. FR-SESS-07.

The observability half (a backgrounded run stays visible through `loom status`) is verified
manually per the SRS; what is unit-testable is the runner itself: it executes off the calling
thread and reports `alive` honestly, which is what the REPL's run state depends on.
"""

from __future__ import annotations

import threading
import time

from loom.tui.background import BackgroundRun


def test_target_runs_off_the_calling_thread() -> None:
    seen: dict[str, str] = {}
    done = threading.Event()

    def target() -> None:
        seen["thread"] = threading.current_thread().name
        done.set()

    bg = BackgroundRun("build", target)
    assert not bg.alive  # not started yet
    bg.start()

    assert done.wait(2.0), "the background target never ran"
    assert seen["thread"] != threading.current_thread().name
    assert seen["thread"] == "loom-bg-build"


def test_alive_is_true_while_working_then_false() -> None:
    started = threading.Event()
    release = threading.Event()

    def target() -> None:
        started.set()
        release.wait(2.0)

    bg = BackgroundRun("run", target)
    bg.start()
    assert started.wait(2.0)
    assert bg.alive

    release.set()
    deadline = time.monotonic() + 2.0
    while bg.alive and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not bg.alive
