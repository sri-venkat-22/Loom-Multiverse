"""Every phase's output model, plus the agent-side protocol every provider satisfies.

Nothing in here may import a provider SDK. These types are the seam between the loop,
the phases and the adapters.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------- agent protocol


class ToolCall(BaseModel):
    """One tool invocation the model asked for."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("arguments", mode="before")
    @classmethod
    def _parse_json_string(cls, v: Any) -> Any:
        # Qwen intermittently returns the arg object as a JSON string. Normalise here so
        # every adapter and every tool sees a dict.
        if isinstance(v, str):
            if not v.strip():
                return {}
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError as exc:
                raise ValueError(f"tool arguments were a string but not valid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("tool arguments must decode to an object")
            return parsed
        return v


class Response(BaseModel):
    """A single provider completion, normalised."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usd_cost: float = Field(default=0.0, ge=0.0)
    in_tokens: int = Field(default=0, ge=0)
    out_tokens: int = Field(default=0, ge=0)
    raw: Any = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class Provider(Protocol):
    """Anything the agent loop can call. Implemented by providers.py and by FakeLLM."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Response: ...


# --------------------------------------------------------------------------- phase artifacts


class Validation(BaseModel):
    """Output of the Validate phase."""

    idea: str
    verdict: Literal["go", "pivot", "no-go"]
    reasoning: str
    competitors: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class PRD(BaseModel):
    """Output of the Plan phase."""

    product_name: str
    problem: str
    users: list[str] = Field(min_length=1)
    v0_features: list[str] = Field(min_length=1)
    non_goals: list[str] = Field(default_factory=list)
    success_metrics: list[str] = Field(default_factory=list)


class FileSpec(BaseModel):
    """One file the build phase is expected to produce."""

    path: str
    purpose: str
    depends_on: list[str] = Field(default_factory=list)


class Criterion(BaseModel):
    """One gradable statement about the built repo."""

    name: str
    kind: Literal["shell", "judge"]
    weight: float = Field(gt=0.0, le=1.0)
    description: str = ""
    command: str | None = None
    question: str | None = None
    evidence_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kind_has_its_payload(self) -> Criterion:
        if self.kind == "shell" and not self.command:
            raise ValueError(f"shell criterion {self.name!r} needs a command")
        if self.kind == "judge" and not self.question:
            raise ValueError(f"judge criterion {self.name!r} needs a question")
        return self


class Rubric(BaseModel):
    """How the build phase decides it is done."""

    criteria: list[Criterion] = Field(min_length=1)
    threshold: float = Field(ge=0.0, le=1.0)
    hard_fail: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _names_resolve(self) -> Rubric:
        names = [c.name for c in self.criteria]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate criterion names: {sorted(dupes)}")
        missing = [n for n in self.hard_fail if n not in names]
        if missing:
            raise ValueError(f"hard_fail names no such criterion: {missing}")
        return self


class Design(BaseModel):
    """Output of the Design phase, and the only input the Build phase needs."""

    summary: str
    stack: list[str] = Field(default_factory=list)
    scaffold_command: str | None = None
    file_manifest: list[FileSpec] = Field(min_length=1)
    rubric: Rubric

    @model_validator(mode="after")
    def _manifest_is_a_dag(self) -> Design:
        paths = [f.path for f in self.file_manifest]
        dupes = {p for p in paths if paths.count(p) > 1}
        if dupes:
            raise ValueError(f"duplicate paths in file_manifest: {sorted(dupes)}")
        known = set(paths)
        deps = {f.path: [d for d in f.depends_on] for f in self.file_manifest}
        for path, on in deps.items():
            unknown = [d for d in on if d not in known]
            if unknown:
                raise ValueError(f"{path} depends on paths not in the manifest: {unknown}")
        # Kahn: whatever is left after peeling zero-indegree nodes is a cycle.
        remaining = dict(deps)
        while True:
            ready = [p for p, on in remaining.items() if not on]
            if not ready:
                break
            for p in ready:
                del remaining[p]
            for on in remaining.values():
                on[:] = [d for d in on if d not in ready]
        if remaining:
            raise ValueError(f"file_manifest has a dependency cycle among: {sorted(remaining)}")
        return self
