"""FR-WS-01/02/03, SEC-08 — snapshots, squash, and the refusal to touch a dirty tree.

Every test runs against a real git repository in a tmp dir. Mocking git here would test the
mock: the whole value of this module is that `reset_to` behaves the way git actually behaves,
including the parts (untracked files, ignored files) where it does not do what you would guess.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from loom.workspace import DirtyWorkspace, Workspace, WorkspaceError


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.create(tmp_path)


# --------------------------------------------------------------------------- setup


def test_create_makes_a_repo_with_a_baseline_commit(tmp_path: Path) -> None:
    """A repo with no commits at all turns every later operation into a special case. One
    empty root commit removes them all."""
    ws = Workspace.create(tmp_path / "app")
    assert (tmp_path / "app" / ".git").is_dir()
    assert ws.head() is not None
    assert git(ws.root, "log", "--oneline").count("\n") == 0


def test_open_refuses_a_directory_that_is_not_a_repo(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="loom init"):
        Workspace.open(tmp_path)


def test_open_accepts_a_clean_repo(ws: Workspace) -> None:
    assert Workspace.open(ws.root).root == ws.root


# --------------------------------------------------------------------------- FR-WS-02


def test_a_dirty_tree_is_refused_and_the_message_names_the_files(ws: Workspace) -> None:
    """FR-WS-02 — never silently committed. Someone's uncommitted afternoon is in there."""
    (ws.root / "notes.txt").write_text("half-finished thought", encoding="utf-8")
    with pytest.raises(DirtyWorkspace) as caught:
        Workspace.open(ws.root)
    assert "notes.txt" in str(caught.value)
    assert "commit" in str(caught.value) or "stash" in str(caught.value)


def test_a_modified_tracked_file_is_also_dirty(ws: Workspace) -> None:
    (ws.root / "a.py").write_text("one", encoding="utf-8")
    ws.snapshot("turn-1")
    (ws.root / "a.py").write_text("two", encoding="utf-8")
    assert ws.is_dirty()
    with pytest.raises(DirtyWorkspace):
        ws.require_clean()


def test_loom_s_own_state_does_not_count_as_dirty(ws: Workspace) -> None:
    """`.loom/` is ignored by the .gitignore WP-1.5 writes (FR-WS-04). If it counted, every
    second run would refuse to start."""
    loom = ws.root / ".loom"
    (loom / "runs").mkdir(parents=True)
    (loom / ".gitignore").write_text("*\n", encoding="utf-8")
    (loom / "runs" / "events.jsonl").write_text('{"kind":"run_started"}\n', encoding="utf-8")
    assert not ws.is_dirty()
    ws.require_clean()


# --------------------------------------------------------------------------- FR-WS-01


def test_snapshot_commits_the_whole_tree_and_labels_it(ws: Workspace) -> None:
    (ws.root / "a.py").write_text("one", encoding="utf-8")
    snap = ws.snapshot("turn-1")
    assert snap.label == "turn-1"
    assert snap.sha == ws.head()
    assert not ws.is_dirty()
    assert ws.resolve("turn-1") == snap.sha


def test_a_label_survives_a_new_workspace_object(ws: Workspace) -> None:
    """Labels are git refs, not memory: WP-4.4 hands one back as a resume point."""
    (ws.root / "a.py").write_text("one", encoding="utf-8")
    snap = ws.snapshot("turn-1")
    assert Workspace.open(ws.root).resolve("turn-1") == snap.sha


def test_a_turn_that_changed_nothing_still_gets_a_snapshot(ws: Workspace) -> None:
    """One snapshot per turn, always — otherwise "reset to turn 5" sometimes means turn 3."""
    first = ws.snapshot("turn-1")
    second = ws.snapshot("turn-2")
    assert first.sha != second.sha
    assert ws.tree_hash() == ws.tree_hash()


def test_labels_are_validated_before_they_reach_git(ws: Workspace) -> None:
    for bad in ("../../etc", "turn 1", "turn~1", "", "refs/heads/main"):
        with pytest.raises(WorkspaceError, match="label"):
            ws.snapshot(bad)


def test_resolving_an_unknown_label_says_so(ws: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="turn-99"):
        ws.reset_to("turn-99")


# --------------------------------------------------------------------------- reset_to


def test_reset_to_restores_a_modified_file(ws: Workspace) -> None:
    (ws.root / "a.py").write_text("original", encoding="utf-8")
    ws.snapshot("turn-1")
    (ws.root / "a.py").write_text("the model's bad idea", encoding="utf-8")

    ws.reset_to("turn-1")
    assert (ws.root / "a.py").read_text(encoding="utf-8") == "original"


def test_reset_to_restores_a_deleted_file(ws: Workspace) -> None:
    """The done-when, verbatim: the exact tree *including deletions*."""
    (ws.root / "a.py").write_text("original", encoding="utf-8")
    (ws.root / "pkg").mkdir()
    (ws.root / "pkg" / "b.py").write_text("also original", encoding="utf-8")
    ws.snapshot("turn-1")

    (ws.root / "a.py").unlink()
    (ws.root / "pkg" / "b.py").unlink()
    ws.reset_to("turn-1")

    assert (ws.root / "a.py").read_text(encoding="utf-8") == "original"
    assert (ws.root / "pkg" / "b.py").read_text(encoding="utf-8") == "also original"


def test_reset_to_removes_files_created_after_the_snapshot(ws: Workspace) -> None:
    """`git reset --hard` alone leaves untracked files behind — the exact tree, minus the part
    everybody forgets."""
    ws.snapshot("turn-1")
    (ws.root / "junk.py").write_text("half a module", encoding="utf-8")
    (ws.root / "junkdir").mkdir()
    (ws.root / "junkdir" / "more.py").write_text("more", encoding="utf-8")

    ws.reset_to("turn-1")
    assert not (ws.root / "junk.py").exists()
    assert not (ws.root / "junkdir").exists()


def test_reset_to_leaves_loom_s_own_state_alone(ws: Workspace) -> None:
    """A reset that wiped `.loom/` would take the event log of the run doing the resetting."""
    loom = ws.root / ".loom"
    loom.mkdir()
    (loom / ".gitignore").write_text("*\n", encoding="utf-8")
    ws.snapshot("turn-1")
    (loom / "events.jsonl").write_text("history\n", encoding="utf-8")
    (ws.root / "junk.py").write_text("x", encoding="utf-8")

    ws.reset_to("turn-1")
    assert (loom / "events.jsonl").read_text(encoding="utf-8") == "history\n"
    assert not (ws.root / "junk.py").exists()


def test_reset_to_an_earlier_turn_walks_back_several(ws: Workspace) -> None:
    for turn in range(1, 4):
        (ws.root / "a.py").write_text(f"version {turn}", encoding="utf-8")
        ws.snapshot(f"turn-{turn}")
    ws.reset_to("turn-1")
    assert (ws.root / "a.py").read_text(encoding="utf-8") == "version 1"


# --------------------------------------------------------------------------- squash


def test_commit_phase_squashes_the_turn_commits_into_one(ws: Workspace) -> None:
    before = len(git(ws.root, "log", "--oneline").splitlines())
    ws.begin_phase("build")
    for turn in range(1, 5):
        (ws.root / "a.py").write_text(f"version {turn}", encoding="utf-8")
        ws.snapshot(f"turn-{turn}")
    assert len(git(ws.root, "log", "--oneline").splitlines()) == before + 4

    sha = ws.commit_phase("build")

    assert len(git(ws.root, "log", "--oneline").splitlines()) == before + 1
    assert sha == ws.head()
    assert "build" in git(ws.root, "log", "-1", "--pretty=%s")


def test_squashing_changes_the_history_not_the_tree(ws: Workspace) -> None:
    ws.begin_phase("build")
    (ws.root / "a.py").write_text("final", encoding="utf-8")
    ws.snapshot("turn-1")
    (ws.root / "b.py").write_text("also final", encoding="utf-8")
    ws.snapshot("turn-2")
    tree_before = ws.tree_hash()

    ws.commit_phase("build")

    assert ws.tree_hash() == tree_before
    assert (ws.root / "a.py").read_text(encoding="utf-8") == "final"
    assert (ws.root / "b.py").read_text(encoding="utf-8") == "also final"
    assert not ws.is_dirty()


def test_a_custom_message_is_used(ws: Workspace) -> None:
    ws.begin_phase("build")
    ws.snapshot("turn-1")
    ws.commit_phase("build", message="loom(build): a URL shortener with click analytics")
    assert git(ws.root, "log", "-1", "--pretty=%s").endswith("click analytics")


def test_commit_phase_without_begin_phase_is_a_clear_error(ws: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="begin_phase"):
        ws.commit_phase("build")


def test_a_phase_base_survives_a_new_workspace_object(ws: Workspace) -> None:
    """So a resumed process can still squash the phase it interrupted."""
    ws.begin_phase("build")
    ws.snapshot("turn-1")
    reopened = Workspace.open(ws.root)
    reopened.commit_phase("build")
    assert "build" in git(ws.root, "log", "-1", "--pretty=%s")


def test_squashing_refuses_when_the_base_is_not_an_ancestor_of_head(ws: Workspace) -> None:
    """FR-WS-03 — never rewrite history outside the snapshots Loom created. If HEAD has moved
    somewhere unrelated, a soft reset would silently delete whatever is between."""
    ws.begin_phase("build")
    ws.snapshot("turn-1")
    git(ws.root, "checkout", "-q", "-b", "elsewhere", ws.resolve("base/build"))
    git(ws.root, "commit", "--allow-empty", "-q", "-m", "someone else's work")
    git(ws.root, "update-ref", "refs/loom/base/build", ws.resolve("turn-1"))

    with pytest.raises(WorkspaceError, match="ancestor"):
        ws.commit_phase("build")


# --------------------------------------------------------------------------- tree_hash


def test_tree_hash_is_stable_for_an_unchanged_tree(ws: Workspace) -> None:
    """WP-4.2 memoizes graded criteria on this — a hash that moved on its own would silently
    re-run every judge call, which is the one cost that grows without anyone noticing."""
    (ws.root / "a.py").write_text("one", encoding="utf-8")
    first = ws.tree_hash()
    assert ws.tree_hash() == first


def test_tree_hash_changes_when_a_file_changes(ws: Workspace) -> None:
    (ws.root / "a.py").write_text("one", encoding="utf-8")
    before = ws.tree_hash()
    (ws.root / "a.py").write_text("two", encoding="utf-8")
    assert ws.tree_hash() != before


def test_tree_hash_covers_uncommitted_work(ws: Workspace) -> None:
    """It is asked for mid-turn, before any snapshot has been taken."""
    before = ws.tree_hash()
    (ws.root / "new.py").write_text("x", encoding="utf-8")
    assert ws.tree_hash() != before


def test_the_same_content_hashes_the_same_in_two_repos(tmp_path: Path) -> None:
    hashes = []
    for name in ("one", "two"):
        ws = Workspace.create(tmp_path / name)
        (ws.root / "a.py").write_text("identical", encoding="utf-8")
        hashes.append(ws.tree_hash())
    assert hashes[0] == hashes[1]


# --------------------------------------------------------------------------- FR-WS-03, SEC-08


def test_the_workspace_cannot_push(ws: Workspace) -> None:
    """FR-WS-03, SEC-09 — checked on the parsed source, so a helper that grew one would fail."""
    import ast

    source = (Path(__file__).resolve().parent.parent / "loom/workspace.py").read_text()
    literals = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "push" not in literals
    assert not any(hasattr(ws, name) for name in ("push", "publish", "remote_add"))


def test_every_agent_turn_is_revertible(ws: Workspace) -> None:
    """SEC-08 stated as a test: whatever the model did on turn N, turn N-1 is still on disk."""
    history = []
    for turn in range(1, 6):
        (ws.root / "app.py").write_text(f"turn {turn}", encoding="utf-8")
        history.append(ws.snapshot(f"turn-{turn}"))

    (ws.root / "app.py").write_text("rm -rf equivalent", encoding="utf-8")
    (ws.root / "surprise.py").write_text("unexpected", encoding="utf-8")

    for turn, snap in enumerate(history, start=1):
        ws.reset_to(snap.label)
        assert (ws.root / "app.py").read_text(encoding="utf-8") == f"turn {turn}"
        assert not (ws.root / "surprise.py").exists()


# --------------------------------------------------------------------------- .loom/ is not ours


def test_loom_state_is_excluded_even_with_no_gitignore(ws: Workspace) -> None:
    """FR-WS-04 writes the `.gitignore`, but this module does not get to depend on that.

    Without the pathspec, a project whose `.loom/` was not ignored would have its event log
    committed into every snapshot and then rolled back by the reset that reads it.
    """
    loom = ws.root / ".loom"
    loom.mkdir()
    (loom / "events.jsonl").write_text("run history\n", encoding="utf-8")

    assert not ws.is_dirty(), "Loom's own state counted as the user's uncommitted work"

    (ws.root / "app.py").write_text("v1", encoding="utf-8")
    ws.snapshot("turn-1")
    assert git(ws.root, "ls-files").splitlines() == ["app.py"]

    (loom / "events.jsonl").write_text("run history\nmore\n", encoding="utf-8")
    before = ws.tree_hash()
    (loom / "events.jsonl").write_text("run history\nmore\nand more\n", encoding="utf-8")
    assert ws.tree_hash() == before, "a growing event log busts WP-4.2's memo on every round"

    ws.reset_to("turn-1")
    assert (loom / "events.jsonl").exists()
