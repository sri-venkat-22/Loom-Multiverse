"""Run the Design's skeleton command before the agent's first turn. FR-BUILD-06, WP-4.3.

A turn spent writing `pyproject.toml` is a turn not spent on the product, and it is a turn the
model can get wrong. The command is deterministic, so run it deterministically.

`scaffold_command` comes out of a model-written Design, so it goes through the same guard as
every other shell command the agent runs — and a refusal is a result, not an exception: a bad
scaffold command should degrade to "the agent writes the boilerplate itself", never to a crash.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from loom.agent.tools.bash import bash_tool

#: Generous: `uv init` is instant but a blueprint's scaffold may fetch a template.
SCAFFOLD_TIMEOUT = 180.0


class ScaffoldResult(BaseModel):
    """What happened, in a form the build phase can log and the model can be told about."""

    command: str | None = None
    ran: bool = False
    ok: bool = True
    output: str = ""

    @property
    def summary(self) -> str:
        if not self.ran:
            return "no scaffold command; starting from an empty workspace"
        return f"scaffold {'succeeded' if self.ok else 'FAILED'}: {self.command}"


async def scaffold(
    command: str | None,
    *,
    workspace: Path,
    timeout: float = SCAFFOLD_TIMEOUT,
) -> ScaffoldResult:
    """Run `command` in `workspace`. No command is not a failure — it is a plain empty repo."""
    if not command or not command.strip():
        return ScaffoldResult(command=command, ran=False, ok=True)

    runner = bash_tool(Path(workspace), timeout=timeout)
    try:
        output = await runner.handler(command=command, timeout=None)
    except Exception as exc:  # noqa: BLE001 - a denied or malformed command is a result
        return ScaffoldResult(
            command=command, ran=True, ok=False, output=f"{type(exc).__name__}: {exc}"
        )
    return ScaffoldResult(command=command, ran=True, ok=output.startswith("exit 0"), output=output)
