"""Validate → Plan → Design → Build, with a gate between each. WP-3.6.

FR-PIPE-01…04, FR-VAL-04, FR-GATE-02, FR-HEADLESS-01/04, FR-COST-03/06.

The four phases are not four special cases. Three of them are `Phase` subclasses and the fourth
is `run_build`, and the loop below treats them the same way: produce an artifact, show it to
whoever is reviewing, and act on what they say. Everything that differs between them lives in
`_inputs_for` and in the two lines that call the build.

Two invariants this module exists to hold:

* **FR-PIPE-02 — a phase reads artifacts, never transcripts.** `_inputs_for` can only return
  validated artifacts, loaded from disk or produced upstream in this same run. There is no
  parameter here that could carry a transcript, which is why there is no test that could catch
  us doing it by accident: it is not reachable.
* **FR-PIPE-03 — the run ceiling is checked before the call.** `RunBudget` is one object shared
  by every phase's provider, so "across all phases" is structural rather than four call sites
  remembering to add up.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from loom.agent.tools.fs import atomic_write
from loom.cache import PhaseCache, config_slice
from loom.config import Config
from loom.contracts import PRD, Design, Provider, Response, Validation
from loom.gates import AutoApprove, Gate
from loom.phases.base import Phase, PhaseError, PhaseOutcome, artifact_path, load_artifact
from loom.phases.build import BuildResult, run_build
from loom.phases.design import DesignPhase
from loom.phases.plan import PlanPhase
from loom.phases.validate import ValidatePhase
from loom.session import Session

#: The order. Also the vocabulary `--from` / `--to` and `/phase` accept.
PHASES: tuple[str, ...] = ("validate", "plan", "design", "build")

#: Which artifact model each phase's file on disk holds. FR-PIPE-04 loads through this.
ARTIFACT_MODELS: dict[str, type[BaseModel]] = {
    "validate": Validation,
    "plan": PRD,
    "design": Design,
}

#: FR-COST-06 — warn once, at this fraction of the run ceiling.
WARN_AT = 0.80

#: FR-GATE-02 — a reviewer who rejects the same phase forever is in a loop with a model. Two
#: re-runs is enough to fix a real complaint; past that the answer is `edit`.
MAX_REJECTS = 2

#: Turns a research phase gets, regardless of what the build is allowed. Validate, Plan and
#: Design produce one JSON object; the only thing more turns buys is more searching, and a
#: real run spent sixteen of them on a search tool that was returning nothing. `max_turns` is
#: sized for a build — forty turns of writing and testing code — and handing that same number
#: to a phase that cannot write a file is how a research phase becomes the expensive one.
SHAPE_A_TURNS = 20

Status = Literal[
    "passed", "abandoned", "budget_exhausted", "stalled", "blocked", "invalid", "no-go"
]


class BudgetExceeded(RuntimeError):
    """FR-PIPE-03 — raised *before* a provider call, never after one."""


class UnattendedWithoutBudget(ValueError):
    """FR-HEADLESS-04 — refusing to run unattended without a ceiling is deliberate."""


class RunBudget:
    """The run-level USD ceiling, shared across every phase's provider."""

    def __init__(self, ceiling: float, *, on_event: Callable[..., Any] | None = None) -> None:
        self.ceiling = ceiling
        self.spent = 0.0
        self.warned = False
        self.on_event = on_event

    @property
    def remaining(self) -> float:
        return max(0.0, self.ceiling - self.spent)

    def check(self) -> None:
        if self.spent >= self.ceiling:
            raise BudgetExceeded(
                f"run budget of ${self.ceiling:.2f} is spent (${self.spent:.4f}). Raise it with "
                "--budget and resume, or accept the run as it stands."
            )

    def add(self, usd: float) -> None:
        self.spent += usd
        if not self.warned and self.spent >= self.ceiling * WARN_AT:
            self.warned = True
            _emit(
                self.on_event,
                "budget_warning",
                spent=round(self.spent, 6),
                ceiling=self.ceiling,
                remaining=round(self.remaining, 6),
            )


class BudgetGuard:
    """A `Provider` that checks the run ceiling before delegating, and adds the cost after.

    A decorator rather than a check inside every phase: there is exactly one place a provider
    call happens from Loom's side, and this is the wrapper around it.
    """

    def __init__(self, inner: Provider, budget: RunBudget) -> None:
        self.inner = inner
        self.budget = budget

    @property
    def model(self) -> str:
        """Passed through — the phase cache keys on it (FR-PIPE-05)."""
        return str(getattr(self.inner, "model", ""))

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> Response:
        self.budget.check()
        response = await self.inner.complete(messages, tools)
        self.budget.add(response.usd_cost)
        return response


class PipelineResult(BaseModel):
    """The run, as the CLI and the terminal both read it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    status: Status
    #: The phases that produced an artifact in this run — not necessarily approved ones.
    phases_run: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    validation: Validation | None = None
    prd: PRD | None = None
    design: Design | None = None
    build: BuildResult | None = None
    usd_spent: float = 0.0
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def phase_range(start: str = "validate", stop: str = "build") -> list[str]:
    """The phases from `start` to `stop`, inclusive. Raises on a name that is not a phase."""
    for name in (start, stop):
        if name not in PHASES:
            raise ValueError(f"unknown phase {name!r}; expected one of {', '.join(PHASES)}")
    lo, hi = PHASES.index(start), PHASES.index(stop)
    if lo > hi:
        raise ValueError(f"{start!r} comes after {stop!r} in the pipeline")
    return list(PHASES[lo : hi + 1])


def read_notes(root: Path) -> str:
    """`.loom/notes.md` — the constraints the founder added with `#`. Trusted input: theirs."""
    path = Path(root) / ".loom" / "notes.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


async def run_pipeline(
    idea: str = "",
    *,
    root: Path,
    provider_factory: Callable[[str], Provider],
    config: Config,
    judge_provider: Provider | None = None,
    session: Session | None = None,
    gate: Gate | None = None,
    start: str = "validate",
    stop: str = "build",
    cache: PhaseCache | None = None,
    ask_user_fn: Callable[..., Any] | None = None,
    unattended: bool = False,
    budget_usd: float | None = None,
    fetcher: Any = None,
) -> PipelineResult:
    """Run the pipeline from `start` to `stop`, gating between phases.

    `provider_factory` takes a phase name and returns a provider bound to it, so spend lands in
    the ledger under the phase that caused it. Tests hand it a lambda over one `FakeLLM`.
    """
    root = Path(root)
    if unattended and budget_usd is None:
        raise UnattendedWithoutBudget(
            "unattended mode requires an explicit --budget. Running a model in a loop with no "
            "ceiling and nobody watching is the one thing this tool will not do for you."
        )

    session = session or Session(root)
    on_event = session.log_event
    gate = gate or AutoApprove(on_event=on_event)
    cache = cache if cache is not None else PhaseCache(root)
    ceiling = budget_usd if budget_usd is not None else config.budget_usd
    budget = RunBudget(ceiling, on_event=on_event)
    slice_ = config_slice(config)

    wanted = phase_range(start, stop)
    result = PipelineResult(run_id=session.run_id, status="passed")
    produced: dict[str, Any] = {}

    on_event("run_started", idea=idea, phases=wanted, budget=budget.ceiling, model=config.model)
    notes = read_notes(root)

    try:
        for name in wanted:
            outcome = await _run_one(
                name,
                idea=idea,
                notes=notes,
                root=root,
                run_id=session.run_id,
                produced=produced,
                provider_factory=provider_factory,
                judge_provider=judge_provider,
                config=config,
                budget=budget,
                cache=cache,
                slice_=slice_,
                gate=gate,
                on_event=on_event,
                ask_user_fn=ask_user_fn,
                result=result,
                fetcher=fetcher,
            )
            # Recorded before the verdict is read: a phase that produced an artifact ran, even
            # if the gate then stopped the pipeline. `loom resume` reads the artifacts, and a
            # `phases_run` that disagreed with what is on disk would be a lie in every report.
            if name in produced:
                result.phases_run.append(name)
            if outcome is not None:
                result.status = outcome
                break
    except BudgetExceeded as exc:
        result.status = "budget_exhausted"
        result.message = str(exc)
    except PhaseError as exc:
        result.status = "invalid"
        result.message = str(exc)

    result.usd_spent = round(budget.spent, 6)
    on_event(
        "run_finished",
        status=result.status,
        phases=result.phases_run,
        usd=result.usd_spent,
    )
    return result


async def _run_one(
    name: str,
    *,
    idea: str,
    notes: str,
    root: Path,
    run_id: str,
    produced: dict[str, Any],
    provider_factory: Callable[[str], Provider],
    judge_provider: Provider | None,
    config: Config,
    budget: RunBudget,
    cache: PhaseCache,
    slice_: Mapping[str, Any],
    gate: Gate,
    on_event: Callable[..., Any],
    ask_user_fn: Callable[..., Any] | None,
    result: PipelineResult,
    fetcher: Any,
) -> Status | None:
    """One phase plus its gate. Returns a terminal status, or `None` to carry on."""
    # FR-PIPE-03 — before the phase, not only before each call inside it. A phase handed a
    # ceiling of zero would exit `budget_exhausted` without ever calling a provider, and the
    # run would report "invalid artifact" for what is plainly an empty wallet.
    budget.check()
    provider = BudgetGuard(provider_factory(name), budget)
    feedback = ""
    previous: BaseModel | None = None

    for attempt in range(MAX_REJECTS + 1):
        if name == "build":
            artifact = await _build(
                produced=produced,
                root=root,
                run_id=run_id,
                provider=provider,
                judge_provider=judge_provider,
                config=config,
                feedback=feedback,
                on_event=on_event,
                ask_user_fn=ask_user_fn,
                result=result,
            )
        else:
            artifact = await _shape_a(
                name,
                idea=idea,
                notes=notes,
                root=root,
                run_id=run_id,
                produced=produced,
                provider=provider,
                config=config,
                budget=budget,
                cache=cache,
                slice_=slice_,
                feedback=feedback,
                on_event=on_event,
                ask_user_fn=ask_user_fn,
                result=result,
                fetcher=fetcher,
            )

        decision = gate(name, artifact, previous)
        if decision.action == "abandon":
            return "abandoned"
        if decision.action == "edit" and decision.artifact is not None:
            # FR-GATE-02 — the human's version is the artifact, and the phase does not re-run.
            artifact = decision.artifact
            _store(name, artifact, produced, root, run_id, result, on_event)
            break
        if decision.action == "approve":
            break

        feedback = decision.feedback
        previous = artifact
        if attempt == MAX_REJECTS:
            return "abandoned"

    return _after_gate(name, produced, result)


def _after_gate(name: str, produced: dict[str, Any], result: PipelineResult) -> Status | None:
    """FR-VAL-04 — a `no-go` does not silently become a plan. The gate above already gave the
    reviewer the chance to override it by approving anyway; this is the unattended path, where
    proceeding to build something the research rejected would be the worst possible default."""
    if name == "validate":
        validation: Validation = produced["validate"]
        if validation.verdict == "no-go":
            result.message = (
                f"Validation returned no-go: {validation.reasoning.strip()[:400]}\n"
                "Re-run with a changed idea, or start from `--from plan` to overrule it."
            )
            return "no-go"
    if name == "build":
        build: BuildResult = produced["build"]
        return None if build.passed else build.status
    return None


async def _shape_a(
    name: str,
    *,
    idea: str,
    notes: str,
    root: Path,
    run_id: str,
    produced: dict[str, Any],
    provider: Provider,
    config: Config,
    budget: RunBudget,
    cache: PhaseCache,
    slice_: Mapping[str, Any],
    feedback: str,
    on_event: Callable[..., Any],
    ask_user_fn: Callable[..., Any] | None,
    result: PipelineResult,
    fetcher: Any,
) -> BaseModel:
    phase = make_phase(name, fetcher=fetcher)
    outcome: PhaseOutcome = await phase.execute(
        provider=provider,
        inputs=inputs_for(
            name, idea=idea, notes=notes, produced=produced, root=root, run_id=run_id
        ),
        root=root,
        run_id=run_id,
        feedback=feedback,
        max_turns=min(config.max_turns, SHAPE_A_TURNS),
        # A research phase that spends the build's budget has nothing left to build with.
        max_usd=min(config.max_usd, max(budget.remaining, 0.0)),
        cache=cache,
        config=slice_,
        on_event=on_event,
        ask_user_fn=ask_user_fn,
    )
    _store(name, outcome.artifact, produced, root, run_id, result, on_event, path=outcome.path)
    return outcome.artifact  # type: ignore[no-any-return]


async def _build(
    *,
    produced: dict[str, Any],
    root: Path,
    run_id: str,
    provider: Provider,
    judge_provider: Provider | None,
    config: Config,
    feedback: str,
    on_event: Callable[..., Any],
    ask_user_fn: Callable[..., Any] | None,
    result: PipelineResult,
) -> BaseModel:
    design = _upstream("design", produced, root, run_id)
    build = await run_build(
        design,
        workspace=root,
        provider=provider,
        judge_provider=judge_provider,
        max_turns=config.max_turns,
        max_usd=config.max_usd,
        judge_model=config.judge_model,
        run_id=run_id,
        feedback=feedback,
        on_event=on_event,
        ask_user_fn=ask_user_fn,
    )
    produced["build"] = build
    result.build = build
    if build.score_path is not None:
        result.artifacts["build"] = str(build.score_path)
    # The gate reviews the grading, which is the only part of a build that fits on a screen.
    return build.score if build.score is not None else build


def make_phase(name: str, *, fetcher: Any = None) -> Phase:
    if name == "validate":
        return ValidatePhase(fetcher=fetcher)
    if name == "plan":
        return PlanPhase()
    if name == "design":
        return DesignPhase()
    raise ValueError(f"{name!r} is not a Shape A phase")


def inputs_for(
    name: str,
    *,
    idea: str,
    notes: str,
    produced: dict[str, Any],
    root: Path,
    run_id: str,
) -> dict[str, Any]:
    """FR-PIPE-02 and FR-PIPE-04 in one function: artifacts only, from memory or from disk."""
    if name == "validate":
        return {"idea": idea, "notes": notes}
    if name == "plan":
        return {"validation": _upstream("validate", produced, root, run_id), "notes": notes}
    if name == "design":
        return {
            "prd": _upstream("plan", produced, root, run_id),
            "validation": produced.get("validate"),
            "notes": notes,
        }
    raise ValueError(f"{name!r} is not a Shape A phase")


def _upstream(name: str, produced: dict[str, Any], root: Path, run_id: str) -> Any:
    """This run's artifact if the phase just ran, otherwise the one on disk, re-validated."""
    if name in produced:
        return produced[name]
    return load_artifact(root, run_id, name, ARTIFACT_MODELS[name])


def _store(
    name: str,
    artifact: Any,
    produced: dict[str, Any],
    root: Path,
    run_id: str,
    result: PipelineResult,
    on_event: Callable[..., Any],
    *,
    path: Path | None = None,
) -> None:
    """Record the artifact, and write it if it came from a human rather than from a phase."""
    produced[name] = artifact
    if path is None:
        path = artifact_path(root, run_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, artifact.model_dump_json(indent=2) + "\n")
        on_event("artifact_written", phase=name, path=str(path), edited=True)
    result.artifacts[name] = str(path)
    setattr(result, {"validate": "validation", "plan": "prd", "design": "design"}[name], artifact)


def _emit(on_event: Callable[..., Any] | None, kind: str, **fields: Any) -> None:
    if on_event is not None:
        on_event(kind, **fields)
