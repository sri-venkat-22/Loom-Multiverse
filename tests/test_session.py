"""FR-SESS-01, FR-SESS-02, FR-SESS-09, NFR-OBS-02, NFR-REL-02."""

from __future__ import annotations

import os
import re
import signal
import threading
from pathlib import Path

import pytest

from loom.session import EVENT_KINDS, Session, list_runs, new_run_id


def test_run_ids_are_unique_and_time_prefixed() -> None:
    """FR-SESS-02 — sortable, collision-resistant, safe as a directory name."""
    ids = [new_run_id() for _ in range(200)]
    assert len(set(ids)) == 200
    assert all(re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{6}", i) for i in ids)
    stamps = [i.split("-")[0] for i in ids]
    assert stamps == sorted(stamps)
    assert not any(set(i) & set("/\\ :") for i in ids)


def test_events_survive_a_process_restart(tmp_path: Path) -> None:
    """FR-SESS-01."""
    s = Session(tmp_path)
    s.log_event("phase_started", phase="validate")
    s.log_event("phase_finished", phase="validate", usd=0.02)

    reopened = Session(tmp_path, s.run_id)  # a fresh object is a fresh process, for our purposes
    events = reopened.read_events()
    assert [e["kind"] for e in events] == ["phase_started", "phase_finished"]
    assert events[1]["usd"] == 0.02
    assert {e["run_id"] for e in events} == {s.run_id}


def test_every_event_carries_run_id_and_a_monotonic_seq(tmp_path: Path) -> None:
    """NFR-OBS-02."""
    s = Session(tmp_path)
    for kind in ("run_started", "phase_started", "turn", "phase_finished", "run_finished"):
        s.log_event(kind)
    events = s.read_events()
    assert [e["seq"] for e in events] == [0, 1, 2, 3, 4]
    assert all(e["run_id"] == s.run_id and e["ts"] for e in events)


def test_seq_continues_across_reopen(tmp_path: Path) -> None:
    """NFR-OBS-02 — monotonic across processes, not just within one."""
    s = Session(tmp_path)
    s.log_event("run_started")
    s.log_event("turn")
    reopened = Session(tmp_path, s.run_id)
    reopened.log_event("run_finished")
    assert [e["seq"] for e in reopened.read_events()] == [0, 1, 2]


def test_a_torn_trailing_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    """NFR-REL-02 — and the next append must not glue itself to the wreckage."""
    s = Session(tmp_path)
    s.log_event("run_started")
    with s.events_path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-01-01", "kind": "half_writ')  # killed mid-write, no newline

    assert [e["kind"] for e in s.read_events()] == ["run_started"]
    s.log_event("run_finished")
    assert [e["kind"] for e in Session(tmp_path, s.run_id).read_events()] == [
        "run_started",
        "run_finished",
    ]


def test_a_torn_line_mid_file_is_skipped(tmp_path: Path) -> None:
    """NFR-REL-02."""
    s = Session(tmp_path)
    s.log_event("run_started")
    with s.events_path.open("a", encoding="utf-8") as fh:
        fh.write("{oops\n")
    s.log_event("run_finished")
    assert [e["kind"] for e in s.read_events()] == ["run_started", "run_finished"]


def test_log_event_returns_the_record_and_survives_odd_values(tmp_path: Path) -> None:
    s = Session(tmp_path)
    event = s.log_event("tool_call", path=tmp_path / "a.py")
    assert event["kind"] == "tool_call" and event["seq"] == 0
    assert s.read_events()[0]["path"] == str(tmp_path / "a.py")


# --------------------------------------------------------------------------- event vocabulary


def test_an_unknown_event_kind_is_refused(tmp_path: Path) -> None:
    """SRS §6 — the vocabulary is closed, so a typo fails at the call site, not silently."""
    s = Session(tmp_path)
    with pytest.raises(ValueError, match="unknown event kind"):
        s.log_event("phase_start")  # the real kind is phase_started
    assert s.read_events() == []


def test_the_documented_vocabulary_is_the_implemented_one() -> None:
    """SRS §6 — every kind the spec lists is accepted."""
    documented = {
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
    assert EVENT_KINDS == documented


# --------------------------------------------------------------------------- interruption


def test_a_signal_is_recorded_before_the_run_unwinds(tmp_path: Path) -> None:
    """FR-SESS-09 — an interrupted run leaves an event saying so, and stays readable."""
    s = Session(tmp_path)
    s.log_event("phase_started", phase="build")

    with pytest.raises(KeyboardInterrupt):
        with s.interruptible(phase="build"):
            os.kill(os.getpid(), signal.SIGINT)

    events = s.read_events()
    assert [e["kind"] for e in events] == ["phase_started", "interrupted"]
    assert events[-1]["signal"] == "SIGINT" and events[-1]["phase"] == "build"


def test_the_previous_handler_is_restored_on_both_paths(tmp_path: Path) -> None:
    """FR-SESS-09 — a second Ctrl-C during cleanup must behave normally."""
    s = Session(tmp_path)
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    with s.interruptible():
        assert signal.getsignal(signal.SIGINT) is not before[signal.SIGINT]
        assert signal.getsignal(signal.SIGTERM) is not before[signal.SIGTERM]
    assert {sig: signal.getsignal(sig) for sig in before} == before

    with pytest.raises(KeyboardInterrupt), s.interruptible():
        os.kill(os.getpid(), signal.SIGINT)
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_interruptible_is_a_no_op_off_the_main_thread(tmp_path: Path) -> None:
    """FR-SESS-09 — signal handlers cannot be installed there; it must not raise either."""
    s = Session(tmp_path)
    failures: list[BaseException] = []

    def body() -> None:
        try:
            with s.interruptible():
                pass
        except BaseException as exc:  # noqa: BLE001 - the point is that nothing escapes
            failures.append(exc)

    thread = threading.Thread(target=body)
    thread.start()
    thread.join()
    assert failures == []


# --------------------------------------------------------------------------- run discovery


def test_no_run_leaves_no_directory(tmp_path: Path) -> None:
    s = Session(tmp_path)
    assert s.read_events() == []
    assert list_runs(tmp_path) == []
    assert Session.latest(tmp_path) is None

    s.log_event("run_started")
    assert list_runs(tmp_path) == [s.run_id]
    latest = Session.latest(tmp_path)
    assert latest is not None and latest.run_id == s.run_id


def test_latest_picks_the_newest_run(tmp_path: Path) -> None:
    for rid in ("20260101T000000-aaa", "20260601T000000-bbb", "20260301T000000-ccc"):
        Session(tmp_path, rid).log_event("run_started")
    latest = Session.latest(tmp_path)
    assert latest is not None and latest.run_id == "20260601T000000-bbb"
