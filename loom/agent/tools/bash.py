"""run_bash — the build phase's one dangerous tool, behind the SEC-02 guard.

FR-TOOL-05: a timeout that fires, a kill that takes the whole process group, and output
truncated head+tail so one runaway `find /` cannot eat the context window.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

from pydantic import Field

from loom.agent.tools.registry import Tool, tool
from loom.security import check_command, redact

#: Total characters of combined output kept. Half from the head (what ran), half from the tail
#: (how it failed). The middle of a 200k-line test log is what nobody reads.
MAX_OUTPUT_CHARS = 40_000

#: Seconds a command may run when neither the caller nor the model says otherwise.
DEFAULT_TIMEOUT = 120.0

#: How long to wait for the pipes to drain after a kill before giving up on partial output.
DRAIN_TIMEOUT = 5.0


def bash_tool(
    root: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    env: Mapping[str, str] | None = None,
    max_output: int = MAX_OUTPUT_CHARS,
) -> Tool:
    """The tool, bound to one workspace root and one timeout ceiling."""
    root = Path(root)
    ceiling = timeout

    @tool
    async def run_bash(
        command: Annotated[str, Field(description="Shell command, run in the workspace root.")],
        timeout: Annotated[int | None, Field(description="Seconds. Capped by the tool.")] = None,
    ) -> str:
        """Run a shell command in the workspace and return its combined output and exit code.

        Commands that leave the workspace, reach the network, or install globally are refused.
        """
        check_command(command, root=root)  # raises CommandDenied; the loop frames it
        limit = min(float(timeout), ceiling) if timeout else ceiling
        return await _run(command, root=root, limit=limit, env=env, max_output=max_output)

    return run_bash


async def _run(
    command: str,
    *,
    root: Path,
    limit: float,
    env: Mapping[str, str] | None,
    max_output: int,
) -> str:
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # Its own process group, so the kill below reaches everything the command started.
        start_new_session=True,
        env=dict(env) if env is not None else None,
    )

    communicate = asyncio.create_task(proc.communicate())
    done, _pending = await asyncio.wait({communicate}, timeout=limit)

    timed_out = not done
    if timed_out:
        _kill_group(proc)
        try:
            # The pipes close once the group is dead, so this returns whatever was produced
            # before the timeout rather than throwing it away.
            stdout, _ = await asyncio.wait_for(asyncio.shield(communicate), DRAIN_TIMEOUT)
        except TimeoutError:
            communicate.cancel()
            stdout = b""
    else:
        stdout, _ = communicate.result()

    output = redact(_truncate(stdout.decode("utf-8", errors="replace"), max_output), env=env)
    if timed_out:
        header = f"ERROR: timed out after {limit:g}s — killed the process group"
    else:
        header = f"exit {proc.returncode}"
    return f"{header}\n{output}" if output else header


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the whole group. Killing only the shell leaves the grandchild holding the pipe."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # already gone, or never ours
        pass


def _truncate(text: str, max_output: int) -> str:
    if len(text) <= max_output:
        return text
    half = max_output // 2
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n\n[... {dropped} characters truncated ...]\n\n{text[-half:]}"
