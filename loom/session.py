"""Append-only JSONL event log. The only place `loom/` is allowed to narrate itself.

FR-SESS-01/02, FR-SESS-09, NFR-OBS-01/02, NFR-REL-02.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

LOOM_DIR = ".loom"

#: The event vocabulary, SRS §6. Closed on purpose: a typo'd kind is invisible to `loom status`
#: and to `/resume`, so it fails at the call site instead. Adding a kind is one line here.
EVENT_KINDS = frozenset(
    {
        "run_started",
        "phase_started",
        "turn",
        "tool_call",
        "tool_result",
        "ask_user",
        "user_answer",
        "artifact_written",
        "gate_opened",
        "gate_decision",
        "graded",
        "retry",
        "budget_warning",
        "phase_finished",
        "interrupted",
        "run_finished",
    }
)


def new_run_id() -> str:
    """Sortable, collision-resistant, and safe as a directory name."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def runs_dir(root: Path) -> Path:
    return Path(root) / LOOM_DIR / "runs"


def notes_path(root: Path) -> Path:
    """`.loom/notes.md` — the user's standing constraints (FR-REPL-10). Project state, so it
    lives beside `runs/` and is git-ignored with the rest of `.loom/`."""
    return Path(root) / LOOM_DIR / "notes.md"


def append_note(root: Path, note: str) -> Path:
    """Append one constraint line (the `#` prefix, FR-REPL-10). Blank input is ignored rather
    than written as an empty bullet."""
    text = note.strip()
    path = notes_path(root)
    if not text:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"- {text}\n")
    return path


def read_notes(root: Path) -> str:
    """The notes file, or "" if there is none. Injected into every phase prompt by `phases/base`
    under a delimited heading (FR-REPL-10)."""
    path = notes_path(root)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def list_runs(root: Path) -> list[str]:
    """Run ids, oldest first. Empty if the project has never been run."""
    d = runs_dir(root)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


class Session:
    """One run's event stream. Events are flushed per call — a crash loses at most the
    event being written, and `read_events` tolerates that torn line."""

    def __init__(self, root: Path, run_id: str | None = None) -> None:
        self.root = Path(root)
        self.run_id = run_id or new_run_id()
        self.dir = runs_dir(self.root) / self.run_id
        self.events_path = self.dir / "events.jsonl"
        self._seq = len(self.read_events())

    @classmethod
    def latest(cls, root: Path) -> Session | None:
        runs = list_runs(root)
        return cls(root, runs[-1]) if runs else None

    def log_event(self, kind: str, **fields: Any) -> dict[str, Any]:
        if kind not in EVENT_KINDS:
            raise ValueError(
                f"unknown event kind {kind!r}. Add it to loom.session.EVENT_KINDS and to "
                f"SRS §6, or use one of: {', '.join(sorted(EVENT_KINDS))}"
            )
        event: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "seq": self._seq,
            "kind": kind,
            **fields,
        }
        self._seq += 1
        self.dir.mkdir(parents=True, exist_ok=True)
        # ponytail: open-per-event. Batch behind a handle only if event volume ever shows up
        # in a profile; this way a hard kill cannot lose buffered history.
        with self.events_path.open("a", encoding="utf-8") as fh:
            # A previous write killed mid-line left no newline. Terminate it, or this event
            # gets glued onto the wreckage and both are lost on read.
            fh.write(self._newline_prefix() + json.dumps(event, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return event

    def _newline_prefix(self) -> str:
        try:
            size = self.events_path.stat().st_size
        except FileNotFoundError:
            return ""
        if not size:
            return ""
        with self.events_path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return "" if fh.read(1) == b"\n" else "\n"

    def read_events(self) -> list[dict[str, Any]]:
        """Every well-formed event. A line that does not parse — a torn trailing write, or a
        torn line mid-file — is skipped, never fatal."""
        if not self.events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.events_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    events.append(parsed)
        return events

    @contextmanager
    def interruptible(self, **fields: Any) -> Iterator[None]:
        """Record an `interrupted` event when a signal arrives, then unwind (FR-SESS-09).

        The handler is restored before the exception propagates, so a second Ctrl-C during
        cleanup behaves normally instead of being swallowed. Outside the main thread signal
        handlers cannot be installed at all, and this becomes a no-op.
        """
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        watched = (signal.SIGINT, signal.SIGTERM)
        previous = {sig: signal.getsignal(sig) for sig in watched}

        def handler(signum: int, frame: FrameType | None) -> None:
            signal.signal(signum, previous[signal.Signals(signum)])
            self.log_event("interrupted", signal=signal.Signals(signum).name, **fields)
            raise KeyboardInterrupt(signal.Signals(signum).name)

        for sig in watched:
            signal.signal(sig, handler)
        try:
            yield
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)
