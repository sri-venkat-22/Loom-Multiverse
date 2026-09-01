"""The Shape A phase: prepare input, run, validate, persist. WP-3.1.

FR-PIPE-02, FR-PIPE-06, FR-ART-01, SEC-03.

Validate, Plan and Design are the same machine with three prompts and three output models. The
machine is: assemble a task from the *artifacts* of the upstream phases (never their
transcripts), run the agent loop, pull a JSON object out of the final message, validate it, and
repair up to three times before giving up with everything it saw.

Two properties are load-bearing:

* **The tool set is the security boundary.** `Phase.tools()` returns `ask_user` and whatever the
  subclass adds. It never returns a filesystem or bash tool, and there is no way to reach one
  from here — these phases ingest untrusted web content (SEC-03).
* **A repair is one provider call, not one more agent loop.** The transcript survives, the
  validation error is appended as a user message, and the model answers again. That keeps a
  no-tool phase at exactly one call on the happy path, which is what FR-SESS-05 measures.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from loom.agent.loop import run_agent_loop
from loom.agent.tools.ask_user import ask_user_tool
from loom.agent.tools.fs import atomic_write
from loom.agent.tools.registry import Tool, ToolRegistry
from loom.cache import PhaseCache, cache_key
from loom.contracts import Provider
from loom.session import read_notes

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

#: FR-PIPE-06 — one honest attempt plus two repairs. A fourth is a different prompt's job.
MAX_ATTEMPTS = 3

#: SRS §5.3 — the artifact each phase writes. The phase name is not the file name because
#: `validation.json` is what a human looking in `.loom/artifacts/` expects to find.
ARTIFACT_FILES = {
    "validate": "validation.json",
    "plan": "prd.json",
    "design": "design.json",
    "build": "score.json",
}

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class Attempt(BaseModel):
    """One rejected output, kept so a `PhaseError` can show its work."""

    n: int
    raw: str
    error: str


class PhaseError(RuntimeError):
    """Three attempts, three unusable outputs. Carries all three (FR-PIPE-06)."""

    def __init__(self, phase: str, attempts: list[Attempt]) -> None:
        self.phase = phase
        self.attempts = attempts
        detail = "\n\n".join(
            f"--- attempt {a.n} ---\n{a.error}\nraw output:\n{a.raw[:2000]}" for a in attempts
        )
        super().__init__(
            f"{phase} did not produce a valid artifact in {len(attempts)} attempts.\n\n{detail}"
        )


class PhaseOutcome(BaseModel):
    """What a phase hands back. `artifact` is already validated — that is the point of it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    phase: str
    artifact: Any
    path: Path | None = None
    turns_used: int = Field(default=0, ge=0)
    usd_spent: float = Field(default=0.0, ge=0.0)
    attempts: int = Field(default=1, ge=0)
    cached: bool = False


def artifact_path(root: Path, run_id: str, phase: str) -> Path:
    """`.loom/artifacts/<run-id>/<file>` — FR-ART-01."""
    name = ARTIFACT_FILES.get(phase, f"{phase}.json")
    return Path(root) / ".loom" / "artifacts" / (run_id or "latest") / name


def load_artifact(root: Path, run_id: str, phase: str, model: type[BaseModel]) -> Any:
    """Read a phase's artifact back off disk, re-validating it.

    FR-ART-03 — artifacts are human-editable, so a hand-edited one is validated again here
    before any downstream phase is allowed to believe it.
    """
    path = artifact_path(root, run_id, phase)
    if not path.is_file():
        raise FileNotFoundError(
            f"no {phase} artifact for run {run_id or 'latest'} at {path}. Run that phase first."
        )
    return model.model_validate_json(path.read_text(encoding="utf-8"))


class Phase(ABC):
    """One Shape A phase. Subclasses supply a name, an output model and a task."""

    name: ClassVar[str]
    artifact_model: ClassVar[type[BaseModel]]
    #: `loom/prompts/<prompt_name>.md`. Overridable per instance for `loom replay --prompt`.
    prompt_name: ClassVar[str]

    def __init__(self, *, prompt_path: Path | None = None) -> None:
        self.prompt_path = prompt_path or PROMPTS / f"{self.prompt_name}.md"

    # ------------------------------------------------------------------ what a subclass writes

    @abstractmethod
    def prepare_input(self, **inputs: Any) -> str:
        """The task message, built from upstream artifacts only (FR-PIPE-02).

        Overrides name their inputs as optional keywords and check for the ones they need. The
        alternative — required keywords — narrows the base signature, and a `Phase` you cannot
        call polymorphically is not worth the three characters it saves.
        """

    def extra_tools(self) -> list[Tool]:
        """Tools beyond `ask_user`. Validate adds the web tools; Plan and Design add nothing."""
        return []

    def refine(self, artifact: Any) -> Any:
        """Last word on a validated artifact, before it is persisted or cached.

        Two jobs, and the difference matters. Rewriting is silent — Plan moves an over-scoped
        feature into `non_goals` rather than making the model pay a round for a judgement call
        we can just make. Raising `ValueError` is not: it lands in the repair loop with the
        message as feedback, which is what a rubric that cannot be graded deserves.
        """
        return artifact

    # ------------------------------------------------------------------ the machine

    @property
    def prompt(self) -> str:
        return self.prompt_path.read_text(encoding="utf-8")

    def tools(self) -> ToolRegistry:
        """SEC-03 — `ask_user` plus whatever the subclass declares, and nothing else. There is
        no branch here that can reach `fs_tools` or `bash_tool`."""
        return ToolRegistry([ask_user_tool(), *self.extra_tools()])

    def system(self) -> str:
        """The prompt file, with the output schema appended so the two cannot drift."""
        schema = json.dumps(self.artifact_model.model_json_schema(), indent=2)
        return (
            f"{self.prompt.rstrip()}\n\n"
            "## Output format\n\n"
            "When you have finished, reply with a single JSON object and nothing else — no "
            "commentary before it, no explanation after it. It must validate against this "
            f"schema:\n\n```json\n{schema}\n```\n\n"
            # ponytail: schema in the prompt, not the provider's native structured-output mode.
            # Native mode is per-provider, and on the one phase that also uses tools (Validate)
            # the two features conflict on most of them. Revisit if the repair rate is ever
            # measurable in the eval harness — the hook is `LiteLLMProvider(extra=...)`.
            "If a field does not apply, use an empty list or an empty string rather than "
            "omitting it or inventing a different field name."
        )

    async def execute(
        self,
        *,
        provider: Provider,
        inputs: Mapping[str, Any] | None = None,
        root: Path | None = None,
        run_id: str = "",
        feedback: str = "",
        max_turns: int = 8,
        max_usd: float = 0.50,
        cache: PhaseCache | None = None,
        config: Mapping[str, Any] | None = None,
        on_event: Callable[..., Any] | None = None,
        ask_user_fn: Callable[..., Any] | None = None,
        persist: bool = True,
    ) -> PhaseOutcome:
        """Run the phase. Raises `PhaseError` only after `MAX_ATTEMPTS` unusable outputs."""
        task = self.prepare_input(**dict(inputs or {}))
        if feedback:
            # FR-GATE-02 — a rejection is appended to the phase *input*, so the model sees what
            # it produced last time only through the reviewer's words, never as a fresh anchor.
            task = f"{task}\n\n## Reviewer feedback on your previous attempt\n\n{feedback}"

        system = self.system()
        if root is not None:
            # FR-REPL-10 — the user's `#` notes are standing constraints on every phase. Injected
            # before the cache key so adding a note is a cache miss, not a silently-ignored file.
            notes = read_notes(root).strip()
            if notes:
                system = (
                    f"{system}\n\n## User constraints\n\n"
                    f"These were set by the user; honour them:\n\n{notes}"
                )
        model_name = str(getattr(provider, "model", ""))
        key = cache_key(phase=self.name, prompt=system, task=task, model=model_name, config=config)

        _emit(on_event, "phase_started", phase=self.name, turns=max_turns, usd_ceiling=max_usd)

        hit = cache.get(key) if cache is not None else None
        if hit is not None:
            try:
                artifact = self.artifact_model.model_validate_json(hit)
            except ValidationError:
                artifact = None  # a cache entry from an older contract is a miss, not a crash
            if artifact is not None:
                outcome = PhaseOutcome(phase=self.name, artifact=artifact, cached=True)
                if persist and root is not None:
                    outcome.path = self._persist(root, run_id, artifact, on_event=on_event)
                _emit(on_event, "phase_finished", phase=self.name, status="cached", usd=0.0)
                return outcome

        result = await run_agent_loop(
            provider=provider,
            system=system,
            task=task,
            tools=self.tools(),
            max_turns=max_turns,
            max_usd=max_usd,
            on_event=on_event,
            ask_user_fn=ask_user_fn,
        )
        messages = result.messages
        usd = result.usd_spent
        attempts: list[Attempt] = []

        for n in range(1, MAX_ATTEMPTS + 1):
            raw = _last_text(messages)
            artifact, error = self._validate(raw)
            if artifact is not None:
                outcome = PhaseOutcome(
                    phase=self.name,
                    artifact=artifact,
                    turns_used=result.turns_used,
                    usd_spent=usd,
                    attempts=n,
                )
                if persist and root is not None:
                    outcome.path = self._persist(root, run_id, artifact, on_event=on_event)
                if cache is not None:
                    cache.put(key, artifact.model_dump_json(indent=2), phase=self.name)
                _emit(
                    on_event,
                    "phase_finished",
                    phase=self.name,
                    status=result.status,
                    turns=result.turns_used,
                    usd=round(usd, 6),
                    attempts=n,
                )
                return outcome

            attempts.append(Attempt(n=n, raw=raw, error=error))
            if n == MAX_ATTEMPTS:
                break
            if result.status == "budget_exhausted" and usd >= max_usd:
                # Out of *money* mid-thought. A repair is another provider call, and buying one
                # past the ceiling is exactly what FR-AGENT-02 exists to stop. The loop reports
                # turn-exhaustion as "budget_exhausted" too, but that case has money to spare and
                # falls through: a phase that spent its turns researching (Validate can burn all
                # of them on search/fetch and never conclude) gets one tool-free call to turn that
                # research into the JSON it was for, rather than hard-failing with an empty reply.
                attempts[-1].error += " (the phase hit its USD ceiling; no repair was attempted)"
                break
            _emit(on_event, "retry", phase=self.name, attempt=n, reason=error.splitlines()[0])
            messages = [*messages, {"role": "user", "content": _repair_message(error)}]
            repair = await provider.complete(messages, None)
            usd += repair.usd_cost
            messages.append({"role": "assistant", "content": repair.text or ""})

        _emit(
            on_event,
            "phase_finished",
            phase=self.name,
            status="invalid",
            usd=round(usd, 6),
            attempts=len(attempts),
        )
        raise PhaseError(self.name, attempts)

    # ------------------------------------------------------------------ internals

    def _validate(self, raw: str) -> tuple[BaseModel | None, str]:
        """A model, or the error text the repair message is built from."""
        data = _json_object(raw)
        if data is None:
            return None, (
                "The reply contained no JSON object. Reply with the object alone, starting "
                "with `{` and ending with `}`."
            )
        try:
            return self.refine(self.artifact_model.model_validate(data)), ""
        except ValidationError as exc:
            lines = [
                f"- {'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
                for e in exc.errors()
            ]
            return None, "The JSON did not validate:\n" + "\n".join(lines)
        except ValueError as exc:
            return None, f"The JSON validated but the phase refused it: {exc}"

    def _persist(
        self,
        root: Path,
        run_id: str,
        artifact: BaseModel,
        *,
        on_event: Callable[..., Any] | None,
    ) -> Path:
        """FR-ART-01 — atomically, after validation, never before."""
        path = artifact_path(root, run_id, self.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, artifact.model_dump_json(indent=2) + "\n")
        _emit(on_event, "artifact_written", phase=self.name, path=str(path))
        return path


def _repair_message(error: str) -> str:
    return (
        f"{error}\n\nReply again with the corrected JSON object and nothing else. Do not "
        "explain the correction; the object is the whole reply."
    )


def _last_text(messages: list[dict[str, Any]]) -> str:
    """The final assistant message. An empty transcript is possible — a loop that exits
    `blocked` on turn one has nothing to say — and yields "", which fails validation with a
    readable error rather than an IndexError."""
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return str(message.get("content") or "")
    return ""


def _json_object(text: str) -> dict[str, Any] | None:
    """The object a model meant to send, out of whatever it wrapped it in.

    Tries the fenced block, the bare text, then the widest brace span. A model that writes
    prose around correct JSON has not failed; making it pay for a repair round would be our
    bug, not its.
    """
    candidates: list[str] = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text.strip())
    widest = _OBJECT.search(text)
    if widest:
        candidates.append(widest.group(0))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _emit(on_event: Callable[..., Any] | None, kind: str, **fields: Any) -> None:
    if on_event is not None:
        on_event(kind, **fields)
