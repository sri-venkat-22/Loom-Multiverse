"""Record a real provider once, replay it forever. FR-EVAL-03.

The point is not to avoid paying for tests — `FakeLLM` already does that. The point is
**provider shape drift**: Qwen returning tool arguments as a JSON string, a response with no
`usage` block, a tool call with no `id`. `FakeLLM` hands the loop a `Response` that is correct by
construction, so it can never catch any of that. A cassette stores the *raw payload* and replays
it through `providers.to_response`, which is the code that would break.

`make cassettes` re-records against live providers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loom.agent.providers import to_response
from loom.contracts import Provider, Response


class CassetteExhausted(RuntimeError):
    """The replay asked for one more completion than was recorded."""


def load_cassette(path: Path) -> list[dict[str, Any]]:
    """Entries in call order. Each is `{model, messages, tools, raw}`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = data["entries"] if isinstance(data, dict) else data
    return [dict(e) for e in entries]


def save_cassette(path: Path, entries: list[dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"entries": entries}, indent=2, default=str)
    path.write_text(body + "\n", encoding="utf-8")


class CassetteProvider:
    """`contracts.Provider` that replays recorded raw payloads, in order, without a network."""

    def __init__(self, entries: list[dict[str, Any]], *, price_table: Any = None) -> None:
        self.entries = list(entries)
        self.price_table = price_table
        self.calls: list[list[dict[str, Any]]] = []

    @classmethod
    def from_file(cls, path: Path, **kwargs: Any) -> CassetteProvider:
        return cls(load_cassette(path), **kwargs)

    @property
    def remaining(self) -> int:
        return len(self.entries) - len(self.calls)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Response:
        index = len(self.calls)
        self.calls.append([dict(m) for m in messages])
        if index >= len(self.entries):
            raise CassetteExhausted(
                f"cassette holds {len(self.entries)} response(s); call {index + 1} was asked for. "
                "Re-record with `make cassettes`."
            )
        entry = self.entries[index]
        return to_response(
            entry["raw"], model=str(entry.get("model", "")), price_table=self.price_table
        )


class Recorder:
    """Wraps a real provider and writes what it saw. Used only by `make cassettes`."""

    def __init__(self, inner: Provider, path: Path, *, model: str) -> None:
        self.inner = inner
        self.path = Path(path)
        self.model = model
        self.entries: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Response:
        response = await self.inner.complete(messages, tools)
        self.entries.append(
            {
                "model": self.model,
                "messages": messages,
                "tools": [t.get("function", {}).get("name") for t in tools or []],
                "raw": response.raw,
            }
        )
        save_cassette(self.path, self.entries)
        return response
