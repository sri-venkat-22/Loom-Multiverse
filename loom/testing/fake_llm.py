"""A scripted Provider. Every unit test in the project runs against this, never a network."""

from __future__ import annotations

import copy
from typing import Any

from loom.contracts import Response


class ScriptExhausted(RuntimeError):
    """The loop asked for one more completion than the test scripted."""


class FakeLLM:
    """Replays `script` in order, recording what it was called with.

    `calls[i]` is a deep copy of the message list as it was on call i, so a test can assert
    on history without the loop's later mutations leaking backwards.
    """

    def __init__(self, script: list[Response]) -> None:
        self.script = list(script)
        self.calls: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[dict[str, Any]] | None] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def remaining(self) -> int:
        return len(self.script) - len(self.calls)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Response:
        index = len(self.calls)
        self.calls.append(copy.deepcopy(messages))
        self.tools_seen.append(copy.deepcopy(tools))
        if index >= len(self.script):
            raise ScriptExhausted(
                f"FakeLLM ran dry: call {index + 1} but only {len(self.script)} response(s) "
                "were scripted"
            )
        return self.script[index]

    def tool_names_seen(self, call: int = -1) -> list[str]:
        """Tool names offered to the model on a given call. Used by tests that assert a phase
        was handed no filesystem or bash tools."""
        specs = self.tools_seen[call] or []
        return [s.get("function", {}).get("name", "") for s in specs]
