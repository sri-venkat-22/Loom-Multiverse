"""Append-only JSONL event log. The only place `loom/` is allowed to narrate itself."""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOOM_DIR = ".loom"


def new_run_id() -> str:
    """Sortable, collision-resistant, and safe as a directory name."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def runs_dir(root: Path) -> Path:
    return Path(root) / LOOM_DIR / "runs"


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
