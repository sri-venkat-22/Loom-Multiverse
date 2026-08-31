"""FR-TOOL-02 (read), FR-TOOL-03 (atomic write), FR-TOOL-04 (str_replace), FR-TOOL-07 (jail)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from loom.agent.tools.fs import MAX_READ_CHARS, fs_tools
from loom.agent.tools.registry import ToolRegistry
from loom.security import JailEscape


@pytest.fixture
def registry(tmp_path: Path) -> ToolRegistry:
    return ToolRegistry(fs_tools(tmp_path))


# --------------------------------------------------------------------------- FR-TOOL-02


async def test_read_file_returns_the_whole_file(registry: ToolRegistry, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert await registry.execute("read_file", {"path": "a.py"}) == "one\ntwo\nthree\n"


async def test_read_file_honours_an_inclusive_one_based_line_range(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    (tmp_path / "a.py").write_text("1\n2\n3\n4\n5\n", encoding="utf-8")
    out = await registry.execute("read_file", {"path": "a.py", "start_line": 2, "end_line": 4})
    assert out == "2\n3\n4\n"


async def test_a_line_range_past_the_end_clamps_rather_than_erroring(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    (tmp_path / "a.py").write_text("1\n2\n", encoding="utf-8")
    assert (
        await registry.execute("read_file", {"path": "a.py", "start_line": 2, "end_line": 99})
        == "2\n"
    )
    assert "ERROR" in await registry.execute("read_file", {"path": "a.py", "start_line": 9})


async def test_a_large_file_truncates_with_an_explicit_marker(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    """FR-TOOL-02 — silent truncation is how a model ends up editing a file it never saw."""
    (tmp_path / "big.txt").write_text("x" * (MAX_READ_CHARS + 5_000), encoding="utf-8")
    out = await registry.execute("read_file", {"path": "big.txt"})
    assert len(out) < MAX_READ_CHARS + 500
    assert "truncated" in out and str(MAX_READ_CHARS + 5_000) in out
    assert "start_line" in out  # it says how to read the rest


async def test_reading_a_missing_file_is_a_readable_error(registry: ToolRegistry) -> None:
    out = await registry.execute("read_file", {"path": "nope.py"})
    assert out.startswith("ERROR") and "nope.py" in out


async def test_reading_a_directory_is_a_readable_error(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    (tmp_path / "pkg").mkdir()
    assert (await registry.execute("read_file", {"path": "pkg"})).startswith("ERROR")


async def test_an_empty_file_says_so(registry: ToolRegistry, tmp_path: Path) -> None:
    (tmp_path / "empty.py").touch()
    assert "empty" in await registry.execute("read_file", {"path": "empty.py"})


# --------------------------------------------------------------------------- FR-TOOL-03


async def test_write_file_creates_parents_and_reports_what_it_did(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    out = await registry.execute("write_file", {"path": "src/app/main.py", "content": "print(1)\n"})
    assert (tmp_path / "src/app/main.py").read_text(encoding="utf-8") == "print(1)\n"
    assert "src/app/main.py" in out


async def test_write_file_overwrites(registry: ToolRegistry, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("old", encoding="utf-8")
    await registry.execute("write_file", {"path": "a.py", "content": "new"})
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "new"


async def test_a_failed_write_leaves_the_original_intact_and_no_debris(
    registry: ToolRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-TOOL-03 — the whole reason for write-then-replace. If the replace fails, the reader
    still sees the old file, whole."""
    target = tmp_path / "a.py"
    target.write_text("original\n", encoding="utf-8")

    def explode(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError, match="disk full"):
        await registry.execute("write_file", {"path": "a.py", "content": "x" * 100})

    assert target.read_text(encoding="utf-8") == "original\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.py"]


# --------------------------------------------------------------------------- FR-TOOL-04

SOURCE = """def add(a, b):
    return a + b


def sub(a, b):
    return a - b
"""


async def test_str_replace_applies_a_unique_match(registry: ToolRegistry, tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(SOURCE, encoding="utf-8")
    out = await registry.execute(
        "str_replace", {"path": "m.py", "old": "return a + b", "new": "return a + b + 0"}
    )
    assert not out.startswith("ERROR")
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == SOURCE.replace(
        "return a + b", "return a + b + 0"
    )


async def test_str_replace_with_no_match_offers_the_closest_lines(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    """FR-TOOL-04 — the near-miss case is the common one: right idea, wrong whitespace."""
    (tmp_path / "m.py").write_text(SOURCE, encoding="utf-8")
    out = await registry.execute(
        "str_replace", {"path": "m.py", "old": "return a+b", "new": "return 0"}
    )
    assert out.startswith("ERROR") and "not found" in out
    assert "return a + b" in out  # the closest line, offered back
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == SOURCE  # untouched


async def test_str_replace_with_several_matches_refuses_and_counts_them(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    (tmp_path / "m.py").write_text(SOURCE, encoding="utf-8")
    out = await registry.execute("str_replace", {"path": "m.py", "old": "a, b", "new": "x, y"})
    assert out.startswith("ERROR") and "ambiguous" in out and "2 matches" in out
    assert "surrounding context" in out
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == SOURCE  # untouched


async def test_str_replace_can_delete_by_replacing_with_nothing(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    (tmp_path / "m.py").write_text(SOURCE, encoding="utf-8")
    await registry.execute(
        "str_replace", {"path": "m.py", "old": "def sub(a, b):\n    return a - b\n", "new": ""}
    )
    assert "sub" not in (tmp_path / "m.py").read_text(encoding="utf-8")


async def test_str_replace_on_a_missing_file_is_a_readable_error(registry: ToolRegistry) -> None:
    out = await registry.execute("str_replace", {"path": "nope.py", "old": "a", "new": "b"})
    assert out.startswith("ERROR") and "nope.py" in out


async def test_str_replace_is_never_a_partial_write(
    registry: ToolRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "m.py").write_text(SOURCE, encoding="utf-8")

    def explode(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError):
        await registry.execute("str_replace", {"path": "m.py", "old": "a + b", "new": "a * b"})
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == SOURCE


# --------------------------------------------------------------------------- FR-TOOL-07


ESCAPES = ["../../etc/passwd", "/etc/passwd", "ok/../../../etc/passwd"]


@pytest.mark.parametrize("path", ESCAPES)
async def test_every_fs_tool_goes_through_the_jail(registry: ToolRegistry, path: str) -> None:
    """FR-TOOL-07 — there is no unguarded path, on any of the three tools."""
    with pytest.raises(JailEscape):
        await registry.execute("read_file", {"path": path})
    with pytest.raises(JailEscape):
        await registry.execute("write_file", {"path": path, "content": "pwned"})
    with pytest.raises(JailEscape):
        await registry.execute("str_replace", {"path": path, "old": "a", "new": "b"})


async def test_a_symlink_out_of_the_workspace_cannot_be_written_through(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    outside = tmp_path.parent / "target_of_the_link.txt"
    outside.write_text("untouched", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    with pytest.raises(JailEscape):
        await registry.execute("write_file", {"path": "link.txt", "content": "pwned"})
    assert outside.read_text(encoding="utf-8") == "untouched"


def test_the_tools_are_exactly_the_three_the_build_phase_needs(tmp_path: Path) -> None:
    assert [t.name for t in fs_tools(tmp_path)] == ["read_file", "write_file", "str_replace"]
