"""The Validate phase. WP-3.2 — FR-VAL-01…04, SEC-03, SEC-04.

The only phase that reads the open internet, and therefore the only one whose input is
adversarial. Two things keep that survivable and both are structural rather than promised: the
tool registry contains no filesystem or bash tool and there is no code path here that could add
one, and every byte a tool returns is wrapped in the untrusted delimiter with the instruction
that it is data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from loom.agent.tools.registry import Tool
from loom.agent.tools.web import UNTRUSTED_SYSTEM_CLAUSE, Fetcher, web_tools
from loom.contracts import Validation
from loom.phases.base import Phase


class ValidatePhase(Phase):
    """Idea in, `Validation` out."""

    name: ClassVar[str] = "validate"
    artifact_model: ClassVar[type] = Validation
    prompt_name: ClassVar[str] = "validate"

    def __init__(self, *, prompt_path: Path | None = None, fetcher: Fetcher | None = None) -> None:
        super().__init__(prompt_path=prompt_path)
        #: Injected in tests so the suite keeps its promise never to open a socket.
        self.fetcher = fetcher

    def extra_tools(self) -> list[Tool]:
        """SEC-03 — search and fetch. No `read_file`, no `write_file`, no `run_bash`."""
        return web_tools(fetcher=self.fetcher)

    def system(self) -> str:
        """SEC-04 — the clause and the delimiter come from the same module, so they cannot
        drift apart into a prompt that promises framing the tools do not apply."""
        return f"{super().system()}\n\n## Untrusted content\n\n{UNTRUSTED_SYSTEM_CLAUSE}\n"

    def prepare_input(self, *, idea: str = "", notes: str = "", **_: Any) -> str:
        if not idea.strip():
            raise ValueError("the validate phase needs an idea to research")
        task = f"# The idea\n\n{idea.strip()}\n"
        if notes.strip():
            # `.loom/notes.md`, the constraints a founder added with `#`. Theirs, so trusted.
            task += f"\n## Constraints from the founder\n\n{notes.strip()}\n"
        return task

    def refine(self, artifact: Any) -> Any:
        """The idea travels in the artifact so that Plan never has to read this transcript
        (FR-PIPE-02), and so an artifact on disk is readable without its run."""
        return artifact
