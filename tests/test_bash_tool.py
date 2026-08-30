"""FR-TOOL-05 — timeout, process-group kill, head+tail truncation. SEC-02 via the same guard."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from loom.agent.tools.bash import MAX_OUTPUT_CHARS, bash_tool
from loom.agent.tools.registry import ToolRegistry
from loom.security import CommandDenied


@pytest.fixture
def registry(tmp_path: Path) -> ToolRegistry:
    return ToolRegistry([bash_tool(tmp_path, timeout=3.0)])


async def test_a_command_runs_in_the_workspace_and_reports_its_exit_code(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    (tmp_path / "marker.txt").write_text("hi", encoding="utf-8")
    out = await registry.execute("run_bash", {"command": "ls"})
    assert "marker.txt" in out
    assert "exit 0" in out


async def test_a_failing_command_surfaces_its_output_and_code(registry: ToolRegistry) -> None:
    out = await registry.execute("run_bash", {"command": "echo boom >&2; exit 3"})
    assert "exit 3" in out and "boom" in out


async def test_stdout_and_stderr_are_interleaved_in_one_stream(registry: ToolRegistry) -> None:
    out = await registry.execute("run_bash", {"command": "echo one; echo two >&2"})
    assert "one" in out and "two" in out


async def test_the_command_runs_relative_to_the_workspace_root(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    await registry.execute("run_bash", {"command": "echo made > made.txt"})
    assert (tmp_path / "made.txt").read_text(encoding="utf-8").strip() == "made"


# --------------------------------------------------------------------------- FR-TOOL-05


async def test_a_slow_command_times_out_and_leaves_no_orphan(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    """FR-TOOL-05 — a 10s sleep dies at the timeout, *and* so does the child it backgrounded.

    Killing only the shell is the bug this test exists for: the grandchild survives, holds the
    pipe open, and the next `pytest` in that workspace is racing a process nobody can see.
    """
    started = time.monotonic()
    out = await registry.execute(
        "run_bash",
        {"command": "sleep 30 & echo $! > child.pid; sleep 10", "timeout": 1},
    )
    elapsed = time.monotonic() - started

    assert "timed out" in out and "1s" in out
    assert elapsed < 8, f"the timeout did not fire: {elapsed:.1f}s"

    child = int((tmp_path / "child.pid").read_text().strip())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


async def test_the_per_call_timeout_cannot_exceed_the_tool_ceiling(tmp_path: Path) -> None:
    """A model that asks for a 3600s timeout does not get one."""
    registry = ToolRegistry([bash_tool(tmp_path, timeout=0.5)])
    started = time.monotonic()
    out = await registry.execute("run_bash", {"command": "sleep 20", "timeout": 600})
    assert "timed out" in out
    assert time.monotonic() - started < 8


async def test_large_output_truncates_head_and_tail_with_a_marker(registry: ToolRegistry) -> None:
    """Head *and* tail: the first lines say what ran, the last say how it failed. The middle is
    what nobody reads."""
    out = await registry.execute(
        "run_bash",
        {"command": "python3 -c \"print('FIRSTLINE'); print('y' * 200000); print('LASTLINE')\""},
    )
    assert len(out) < MAX_OUTPUT_CHARS + 2_000
    assert "FIRSTLINE" in out and "LASTLINE" in out
    assert "truncated" in out


# --------------------------------------------------------------------------- SEC-02


async def test_a_denied_command_never_runs(registry: ToolRegistry, tmp_path: Path) -> None:
    """The same guard the agent's own path uses — one deny-list, not two (FR-REPL-09 later
    depends on that being true)."""
    with pytest.raises(CommandDenied, match="git push"):
        await registry.execute("run_bash", {"command": "git push origin main"})
    with pytest.raises(CommandDenied):
        await registry.execute("run_bash", {"command": "cat /etc/passwd"})
    assert not (tmp_path / "anything").exists()


async def test_secrets_in_output_are_redacted(tmp_path: Path) -> None:
    """SEC-06 — `env` is the shortest path from a user's API key into a model transcript."""
    env = {"ANTHROPIC_API_KEY": "sk-ant-super-secret-value", "PATH": os.environ["PATH"]}
    registry = ToolRegistry([bash_tool(tmp_path, env=env)])
    out = await registry.execute("run_bash", {"command": "echo $ANTHROPIC_API_KEY"})
    assert "sk-ant-super-secret-value" not in out
    assert "[redacted]" in out


async def test_the_tool_is_named_run_bash(tmp_path: Path) -> None:
    assert bash_tool(tmp_path).name == "run_bash"
