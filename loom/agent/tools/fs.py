"""read_file, write_file, str_replace — the only three file tools the build phase gets.

FR-TOOL-02/03/04, FR-TOOL-07. Every path goes through `security.resolve_in_jail`; there is no
second code path that touches the filesystem.

Editing is exact-string replace, unique-match-or-error (build plan §0 change 4). A diff-hunk
applier fails *quietly* at about a fifth of hunks; this fails loudly and tells the model what to
do about it, in about a tenth of the code.
"""

from __future__ import annotations

import difflib
import os
import tempfile
from pathlib import Path
from typing import Annotated

from pydantic import Field

from loom.agent.tools.registry import Tool, tool
from loom.security import resolve_in_jail

#: Above this a read is truncated. Roughly 15k tokens — big enough for any file a generated repo
#: has, small enough that one `read_file` cannot eat the context window.
MAX_READ_CHARS = 60_000

#: How many near-miss lines a failed `str_replace` offers back.
CLOSEST = 3

PathArg = Annotated[str, Field(description="Path relative to the workspace root.")]


def fs_tools(root: Path) -> list[Tool]:
    """The three tools, bound to one workspace root."""
    root = Path(root)

    @tool
    def read_file(
        path: PathArg,
        start_line: Annotated[int | None, Field(description="1-based, inclusive.")] = None,
        end_line: Annotated[int | None, Field(description="1-based, inclusive.")] = None,
    ) -> str:
        """Read a file from the workspace, optionally a line range of it.

        Large files are truncated with a marker saying so.
        """
        target = resolve_in_jail(root, path)
        if target.is_dir():
            return f"ERROR: {path} is a directory, not a file."
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return f"ERROR: no such file: {path}"
        except OSError as exc:
            return f"ERROR: could not read {path}: {exc}"

        if not text:
            return f"({path} is an empty file)"

        if start_line is not None or end_line is not None:
            lines = text.splitlines(keepends=True)
            start = max(1, start_line or 1)
            end = min(len(lines), end_line if end_line is not None else len(lines))
            if start > len(lines):
                return f"ERROR: {path} has {len(lines)} lines, so line {start} does not exist."
            text = "".join(lines[start - 1 : end])

        return _truncate_read(text, path)

    @tool
    def write_file(
        path: PathArg,
        content: Annotated[str, Field(description="The complete new contents of the file.")],
    ) -> str:
        """Write a file, creating parent directories as needed. Replaces any existing file."""
        target = resolve_in_jail(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, content)
        return f"wrote {len(content)} characters to {path}"

    @tool
    def str_replace(
        path: PathArg,
        old: Annotated[str, Field(description="Exact text to replace. Must appear exactly once.")],
        new: Annotated[str, Field(description="Replacement text. Empty string deletes.")],
    ) -> str:
        """Replace one exact, unique occurrence of `old` with `new`.

        If `old` appears zero times or more than once, nothing is written and the error says
        which — include more surrounding lines to disambiguate.
        """
        target = resolve_in_jail(root, path)
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return f"ERROR: no such file: {path}"
        except (OSError, UnicodeDecodeError) as exc:
            return f"ERROR: could not read {path}: {exc}"

        count = text.count(old)
        if count == 0:
            return _not_found(path, old, text)
        if count > 1:
            return (
                f"ERROR: ambiguous — `old` matches {count} matches in {path}. Add surrounding "
                "context lines so exactly one occurrence matches."
            )

        _atomic_write(target, text.replace(old, new, 1))
        return f"replaced 1 occurrence in {path}"

    return [read_file, write_file, str_replace]


def _truncate_read(text: str, path: str) -> str:
    if len(text) <= MAX_READ_CHARS:
        return text
    return (
        text[:MAX_READ_CHARS]
        + f"\n\n[truncated: showed {MAX_READ_CHARS} of {len(text)} characters of {path}. "
        "Read the rest with start_line / end_line.]"
    )


def _not_found(path: str, old: str, text: str) -> str:
    """FR-TOOL-04 — the near-miss case. Right idea, wrong whitespace, every time."""
    probe = next((line for line in old.splitlines() if line.strip()), old)
    lines = text.splitlines()
    close = difflib.get_close_matches(probe, lines, n=CLOSEST, cutoff=0.4)
    if not close:
        return f"ERROR: not found — `old` does not appear in {path}."
    numbered = "\n".join(f"  line {lines.index(c) + 1}: {c}" for c in close)
    return (
        f"ERROR: not found — `old` does not appear in {path}. The closest lines in the file "
        f"are:\n{numbered}\nCopy one of them exactly, including indentation."
    )


def _atomic_write(target: Path, content: str) -> None:
    """FR-TOOL-03 — write beside the target, fsync, then rename over it.

    `os.replace` is atomic within a filesystem, and the temp file is in the same directory so it
    always is one. A reader either sees the whole old file or the whole new one.
    """
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
