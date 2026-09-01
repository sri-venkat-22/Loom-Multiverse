"""WP-8.5 — `/rewind`. FR-SESS-08.

Two halves, tested apart: `truncate_transcript` is a pure function over a message list (fast, no
git), and `rewind` restores a real git workspace (slow, real repository — mocking git here would
only test the mock, exactly as `test_workspace.py` argues).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.replay import RewindResult, rewind, rewind_targets, truncate_transcript
from loom.session import runs_dir
from loom.workspace import Workspace


def _transcript() -> list[dict]:
    """A build message list the shape `agent/loop.py` produces: two tool-calling turns, then a
    plain-text turn whose failed grade appended a feedback message, then one more turn."""
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},  # turn 1
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "2"}]},  # turn 2
        {"role": "tool", "tool_call_id": "2", "content": "ok"},
        {"role": "assistant", "content": "done"},  # turn 3 (graded, failed)
        {"role": "user", "content": "feedback"},
        {"role": "assistant", "content": "done again"},  # turn 4
    ]


# --------------------------------------------------------------------------- truncate_transcript


def test_turn_zero_keeps_only_the_priming() -> None:
    # Turn 0 == the `scaffold` snapshot: before the model's first turn, only system + task remain.
    assert truncate_transcript(_transcript(), 0) == _transcript()[:2]


def test_truncate_cuts_at_the_next_turn_boundary() -> None:
    msgs = _transcript()
    # Turn 1 keeps its assistant message and its tool result, and nothing of turn 2.
    assert truncate_transcript(msgs, 1) == msgs[:4]
    # Turn 2 keeps through its tool result.
    assert truncate_transcript(msgs, 2) == msgs[:6]
    # Turn 3 keeps its assistant message and the feedback that grading appended after it.
    assert truncate_transcript(msgs, 3) == msgs[:8]


def test_a_turn_at_or_past_the_end_keeps_everything() -> None:
    msgs = _transcript()
    assert truncate_transcript(msgs, 4) == msgs
    assert truncate_transcript(msgs, 99) == msgs


def test_one_assistant_message_per_turn_is_the_counting_key() -> None:
    # The count is of assistant messages, so tool and user messages between them do not shift it.
    msgs = _transcript()
    assistants = [i for i, m in enumerate(msgs) if m["role"] == "assistant"]
    assert truncate_transcript(msgs, 2) == msgs[: assistants[2]]


# --------------------------------------------------------------------------- rewind_targets


@pytest.mark.slow
def test_rewind_targets_are_turn_snapshots_earliest_first(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path)
    for label in ("turn-2", "scaffold", "turn-10", "turn-1"):
        ws.snapshot(label)
    ws.begin_phase("build")  # a base ref, which is not a turn snapshot and must not appear
    # Natural order by turn number, not git's lexical ref order (which puts turn-10 before turn-2).
    assert rewind_targets(tmp_path, "r") == ["scaffold", "turn-1", "turn-2", "turn-10"]


def test_rewind_targets_is_empty_without_a_repo(tmp_path: Path) -> None:
    assert rewind_targets(tmp_path, "r") == []


# --------------------------------------------------------------------------- rewind


@pytest.mark.slow
def test_rewind_restores_the_tree_and_truncates_the_transcript(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path)
    (tmp_path / "app.py").write_text("turn 1", encoding="utf-8")
    ws.snapshot("turn-1")
    (tmp_path / "app.py").write_text("turn 2", encoding="utf-8")
    ws.snapshot("turn-2")

    run_id = "r1"
    tdir = runs_dir(tmp_path) / run_id
    tdir.mkdir(parents=True)
    msgs = _transcript()
    (tdir / "transcript.json").write_text(json.dumps(msgs), encoding="utf-8")

    # the model made a mess after turn-2
    (tmp_path / "app.py").write_text("garbage", encoding="utf-8")
    (tmp_path / "junk.py").write_text("half a module", encoding="utf-8")

    result = rewind(tmp_path, run_id, "turn-1")

    assert isinstance(result, RewindResult)
    assert result.turn == 1
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "turn 1"  # tree restored
    assert not (tmp_path / "junk.py").exists()  # untracked spill cleaned too

    kept = json.loads((tdir / "transcript.json").read_text(encoding="utf-8"))
    assert kept == truncate_transcript(msgs, 1)
    assert result.messages_kept == len(kept)


@pytest.mark.slow
def test_rewind_survives_a_run_with_no_transcript(tmp_path: Path) -> None:
    # A run interrupted before its transcript was persisted still rewinds the workspace.
    ws = Workspace.create(tmp_path)
    (tmp_path / "app.py").write_text("v1", encoding="utf-8")
    ws.snapshot("turn-1")
    (tmp_path / "app.py").write_text("v2", encoding="utf-8")

    result = rewind(tmp_path, "no-transcript", "turn-1")

    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "v1"
    assert result.messages_kept == 0
    assert result.transcript_path is None


def test_rewind_rejects_a_label_that_is_not_a_turn(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a turn snapshot"):
        rewind(tmp_path, "r", "base/build")
