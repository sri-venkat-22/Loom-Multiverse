"""The project invariants, checked mechanically instead of by review.

NFR-MAINT-02 (core imports no terminal library), NFR-MAINT-03 (only providers.py imports
litellm), NFR-PERF-01 (cold start), NFR-TEST-02 (the network really is blocked).
"""

from __future__ import annotations

import ast
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

LOOM = Path(__file__).resolve().parent.parent / "loom"

#: CLAUDE.md, as amended by SRS §2.5.1. Terminal libraries are allowed in these files only.
TERMINAL_LIBRARIES = {"typer", "rich", "prompt_toolkit", "questionary", "textual", "curses"}
TERMINAL_ALLOWED = {"loom/cli.py", "loom/gates.py", "loom/ui.py"}
TERMINAL_ALLOWED_PACKAGES = {"loom/tui"}

#: CLAUDE.md — every model call goes through one adapter.
PROVIDER_LIBRARIES = {"litellm", "anthropic", "openai"}
PROVIDER_ALLOWED = {"loom/agent/providers.py"}


def _modules() -> list[tuple[str, set[str]]]:
    """(repo-relative path, top-level modules it imports) for every file under loom/."""
    out: list[tuple[str, set[str]]] = []
    for path in sorted(LOOM.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        rel = path.relative_to(LOOM.parent).as_posix()
        out.append((rel, imported))
    return out


def _allowed_terminal(rel: str) -> bool:
    return rel in TERMINAL_ALLOWED or any(
        rel.startswith(pkg + "/") for pkg in TERMINAL_ALLOWED_PACKAGES
    )


def test_the_core_imports_no_terminal_library() -> None:
    """NFR-MAINT-02 — the core must stay drivable as a library, with no TTY."""
    offenders = {
        rel: sorted(imports & TERMINAL_LIBRARIES)
        for rel, imports in _modules()
        if imports & TERMINAL_LIBRARIES and not _allowed_terminal(rel)
    }
    assert offenders == {}, (
        f"terminal libraries outside {sorted(TERMINAL_ALLOWED | TERMINAL_ALLOWED_PACKAGES)}: "
        f"{offenders}"
    )


def test_only_the_adapter_imports_a_provider_sdk() -> None:
    """NFR-MAINT-03."""
    offenders = {
        rel: sorted(imports & PROVIDER_LIBRARIES)
        for rel, imports in _modules()
        if imports & PROVIDER_LIBRARIES and rel not in PROVIDER_ALLOWED
    }
    assert offenders == {}, f"provider SDKs outside {sorted(PROVIDER_ALLOWED)}: {offenders}"


def test_the_guard_would_actually_catch_a_violation(tmp_path: Path) -> None:
    """A guard nobody has seen fail is a guard nobody should trust."""
    tree = ast.parse("import typer\nfrom rich import print\n")
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported & TERMINAL_LIBRARIES == {"typer", "rich"}
    assert not _allowed_terminal("loom/pipeline.py")
    assert _allowed_terminal("loom/tui/repl.py")


def test_the_cli_starts_cold_in_well_under_400ms() -> None:
    """NFR-PERF-01 — litellm and friends must never land on the CLI import path."""
    best = min(_import_ms() for _ in range(3))
    assert best < 400, f"cold import of loom.cli took {best:.0f} ms"


def _import_ms() -> float:
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-c", "import loom.cli"],
        cwd=LOOM.parent,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
    return (time.perf_counter() - started) * 1000


def test_the_network_block_is_on() -> None:
    """NFR-TEST-02 — conftest's autouse fixture, verified rather than assumed."""
    with pytest.raises(RuntimeError, match="NFR-TEST-02"):
        socket.create_connection(("example.com", 80))
    with pytest.raises(RuntimeError, match="NFR-TEST-02"):
        socket.socket().connect(("example.com", 80))
