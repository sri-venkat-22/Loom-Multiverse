from __future__ import annotations

import re
from pathlib import Path

from loom.session import Session, list_runs, new_run_id


def test_run_ids_are_unique_and_time_prefixed() -> None:
    ids = [new_run_id() for _ in range(200)]
    assert len(set(ids)) == 200
    assert all(re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{6}", i) for i in ids)
    stamps = [i.split("-")[0] for i in ids]
    assert stamps == sorted(stamps)


def test_events_survive_a_process_restart(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.log_event("phase_start", phase="validate")
    s.log_event("phase_end", phase="validate", usd=0.02)

    reopened = Session(tmp_path, s.run_id)  # a fresh object is a fresh process, for our purposes
    events = reopened.read_events()
    assert [e["kind"] for e in events] == ["phase_start", "phase_end"]
    assert events[1]["usd"] == 0.02
    assert {e["run_id"] for e in events} == {s.run_id}


def test_seq_continues_across_reopen(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.log_event("a")
    s.log_event("b")
    reopened = Session(tmp_path, s.run_id)
    reopened.log_event("c")
    assert [e["seq"] for e in reopened.read_events()] == [0, 1, 2]


def test_a_torn_trailing_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.log_event("good_one")
    with s.events_path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-01-01", "kind": "half_writ')  # killed mid-write, no newline

    assert [e["kind"] for e in s.read_events()] == ["good_one"]
    s.log_event("after_the_tear")
    assert [e["kind"] for e in Session(tmp_path, s.run_id).read_events()] == [
        "good_one",
        "after_the_tear",
    ]


def test_a_torn_line_mid_file_is_skipped(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.log_event("first")
    with s.events_path.open("a", encoding="utf-8") as fh:
        fh.write("{oops\n")
    s.log_event("third")
    assert [e["kind"] for e in s.read_events()] == ["first", "third"]


def test_log_event_returns_the_record_and_survives_odd_values(tmp_path: Path) -> None:
    s = Session(tmp_path)
    event = s.log_event("tool_call", path=tmp_path / "a.py")
    assert event["kind"] == "tool_call" and event["seq"] == 0
    assert s.read_events()[0]["path"] == str(tmp_path / "a.py")


def test_no_run_leaves_no_directory(tmp_path: Path) -> None:
    s = Session(tmp_path)
    assert s.read_events() == []
    assert list_runs(tmp_path) == []
    assert Session.latest(tmp_path) is None

    s.log_event("started")
    assert list_runs(tmp_path) == [s.run_id]
    latest = Session.latest(tmp_path)
    assert latest is not None and latest.run_id == s.run_id


def test_latest_picks_the_newest_run(tmp_path: Path) -> None:
    for rid in ("20260101T000000-aaa", "20260601T000000-bbb", "20260301T000000-ccc"):
        Session(tmp_path, rid).log_event("x")
    latest = Session.latest(tmp_path)
    assert latest is not None and latest.run_id == "20260601T000000-bbb"
