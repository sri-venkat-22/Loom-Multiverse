"""WP-5.5 — non-TTY line output. FR-HEADLESS-02/03.

Unit level: the two sinks and the factory. The FR verifies that need a whole pipeline
(no ESC byte through a piped run; every `--json` line parses) live in `test_cli_run.py`,
where the pipeline fixtures already exist.
"""

from __future__ import annotations

import io
import json

from loom.ui import JsonLine, TurnLine, event_sink


def _clock() -> object:
    """A deterministic monotonic-ish clock: 0.0, 0.5, 1.0, … one tick per call."""
    ticks = iter(t / 2 for t in range(1000))
    return lambda: next(ticks)


def test_turn_line_writes_one_line_per_turn() -> None:
    out = io.StringIO()
    sink = TurnLine(out, clock=_clock())
    sink({"kind": "phase_started", "phase": "build"})
    sink({"kind": "turn", "turn": 1, "usd": 0.0123, "tool_calls": ["read_file"], "text": False})
    sink({"kind": "turn", "turn": 2, "usd": 0.0456, "tool_calls": [], "text": True})
    lines = out.getvalue().splitlines()
    assert len(lines) == 2
    assert "build" in lines[0] and "turn  1" in lines[0] and "read_file" in lines[0]
    assert "$0.0123" in lines[0]
    assert "text" in lines[1] and "$0.0456" in lines[1]


def test_turn_line_has_no_escape_sequences() -> None:
    """FR-HEADLESS-02 — no ESC byte, no cursor movement."""
    out = io.StringIO()
    sink = TurnLine(out, clock=_clock())
    sink({"kind": "phase_started", "phase": "validate"})
    sink({"kind": "turn", "turn": 1, "usd": 0.0, "tool_calls": ["search_web"], "text": False})
    assert "\x1b" not in out.getvalue()
    assert "\r" not in out.getvalue()


def test_turn_line_ignores_non_turn_events() -> None:
    out = io.StringIO()
    sink = TurnLine(out, clock=_clock())
    sink({"kind": "tool_result", "name": "read_file", "chars": 40})
    sink({"kind": "run_finished", "status": "passed"})
    assert out.getvalue() == ""


def test_turn_line_shows_a_dash_when_a_turn_did_nothing() -> None:
    out = io.StringIO()
    TurnLine(out, clock=_clock())({"kind": "turn", "turn": 1, "usd": 0.0, "text": False})
    assert "—" in out.getvalue()


def test_json_line_emits_the_whole_event_and_parses() -> None:
    """FR-HEADLESS-03 — every line is a JSON object carrying the event verbatim."""
    out = io.StringIO()
    sink = JsonLine(out)
    sink({"kind": "turn", "turn": 1, "usd": 0.01, "seq": 7})
    sink({"kind": "run_finished", "status": "passed"})
    for line in out.getvalue().splitlines():
        json.loads(line)  # raises if any line is not valid JSON
    first = json.loads(out.getvalue().splitlines()[0])
    assert first == {"kind": "turn", "turn": 1, "usd": 0.01, "seq": 7}


def test_json_line_degrades_a_non_serialisable_field_to_text() -> None:
    from pathlib import Path

    out = io.StringIO()
    JsonLine(out)({"kind": "artifact_written", "path": Path("/tmp/x.json")})
    assert json.loads(out.getvalue())["path"] == "/tmp/x.json"


def test_event_sink_picks_the_renderer() -> None:
    out = io.StringIO()
    assert isinstance(event_sink(out, json_mode=True, tty=True), JsonLine)  # --json wins
    assert isinstance(event_sink(out, json_mode=True, tty=False), JsonLine)
    assert isinstance(event_sink(out, json_mode=False, tty=False), TurnLine)  # piped → lines
    assert event_sink(out, json_mode=False, tty=True) is None  # TTY → the live view (8.3)
