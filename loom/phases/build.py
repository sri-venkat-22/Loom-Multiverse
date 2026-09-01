"""The build phase: scaffold, run the agent, grade, feed the failures back, repeat.

FR-BUILD-01/02/03/05, FR-ART-04, SEC-08.

Most of the round loop is not here — `agent/loop.py` already grades on every text-only response
and appends `Score.as_feedback()` when it falls short. What this module owns is the wiring that
makes those rounds safe and inspectable: the scaffold, the tool set, a snapshot per turn, the
persisted score, and the squash on success.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from loom.agent.loop import LoopResult, run_agent_loop
from loom.agent.tools.ask_user import ask_user_tool
from loom.agent.tools.bash import bash_tool

# One implementation of write-then-replace in the tree; `phases/base.py` and `cache.py` use the
# same one. It stays in the tool module because that is where the FR-TOOL-03 test lives.
from loom.agent.tools.fs import atomic_write, fs_tools
from loom.agent.tools.registry import ToolRegistry
from loom.contracts import Design, Provider, Rubric
from loom.ledger import Ledger
from loom.rubric import Memo, Score
from loom.rubric import grade as grade_rubric
from loom.scaffold import ScaffoldResult, scaffold
from loom.workspace import Workspace

PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "build.md"

PHASE = "build"

Status = Literal["passed", "budget_exhausted", "stalled", "blocked"]


class BuildResult(BaseModel):
    """What the pipeline and the terminal both read after a build."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: Status
    score: Score | None = None
    turns_used: int = Field(default=0, ge=0)
    usd_spent: float = Field(default=0.0, ge=0.0)
    rounds: int = Field(default=0, ge=0)
    #: The label to `reset_to` when picking this run back up. FR-BUILD-02.
    snapshot: str = ""
    #: The squashed phase commit, on success only.
    commit: str | None = None
    score_path: Path | None = None
    scaffold: ScaffoldResult | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"


class RubricGrader:
    """Binds a `Rubric` to a workspace: the `Grader` the agent loop expects.

    It also owns the memo, which is the whole reason it is an object rather than a closure —
    the memo has to survive across grading rounds for COST CONTROL 2 to mean anything.
    """

    def __init__(
        self,
        rubric: Rubric,
        *,
        workspace: Path,
        judge_provider: Provider | None = None,
        ledger: Ledger | None = None,
        judge_model: str = "",
        run_id: str = "",
    ) -> None:
        self.rubric = rubric
        self.threshold = rubric.threshold
        self.workspace = Path(workspace)
        self.judge_provider = judge_provider
        self.ledger = ledger
        self.judge_model = judge_model
        self.run_id = run_id
        self.memo: Memo = {}
        self.rounds = 0
        self.last: Score | None = None

    async def grade(self) -> Score:
        self.rounds += 1
        self.last = await grade_rubric(
            self.rubric,
            workspace=self.workspace,
            judge_provider=self.judge_provider,
            memo=self.memo,
            ledger=self.ledger,
            judge_model=self.judge_model,
            run_id=self.run_id,
        )
        return self.last


async def run_build(
    design: Design,
    *,
    workspace: Path,
    provider: Provider,
    judge_provider: Provider | None = None,
    max_turns: int = 25,
    max_usd: float = 1.0,
    ledger: Ledger | None = None,
    judge_model: str = "",
    run_id: str = "",
    feedback: str = "",
    on_event: Callable[..., Any] | None = None,
    ask_user_fn: Callable[..., Any] | None = None,
) -> BuildResult:
    """Build the repo `design` describes, in `workspace`, and grade it against its own rubric.

    `feedback` is what a reviewer said when they rejected the last build (FR-GATE-02). It is
    appended to the task rather than to the design, because the design was already approved and
    a rejected build is a complaint about the code, not about the plan.
    """
    repo = Workspace.open(workspace)  # FR-WS-02: refuses to build over someone's uncommitted work
    repo.begin_phase(PHASE)
    _emit(on_event, "phase_started", phase=PHASE, turns=max_turns, usd_ceiling=max_usd)

    skeleton = await scaffold(design.scaffold_command, workspace=repo.root)
    _emit(on_event, "tool_result", name="scaffold", chars=len(skeleton.output))
    repo.snapshot("scaffold")

    tools = ToolRegistry([*fs_tools(repo.root), bash_tool(repo.root), ask_user_tool()])
    grader = RubricGrader(
        design.rubric,
        workspace=repo.root,
        judge_provider=judge_provider,
        ledger=ledger,
        judge_model=judge_model,
        run_id=run_id,
    )
    snapshots = _Snapshotter(repo, forward=on_event)

    result: LoopResult = await run_agent_loop(
        provider=provider,
        system=PROMPT.read_text(encoding="utf-8"),
        task=_task(design, skeleton, feedback),
        tools=tools,
        rubric=grader,
        max_turns=max_turns,
        max_usd=max_usd,
        on_event=snapshots,
        ask_user_fn=ask_user_fn,
    )

    label = snapshots.close()
    _persist_transcript(workspace, run_id, result)
    build = BuildResult(
        status=result.status,
        score=grader.last,
        turns_used=result.turns_used,
        usd_spent=result.usd_spent,
        rounds=grader.rounds,
        snapshot=label,
        scaffold=skeleton,
    )

    if grader.last is not None:
        build.score_path = _persist_score(repo.root, run_id, grader.last)
        _emit(on_event, "artifact_written", phase=PHASE, path=str(build.score_path))

    if build.passed:
        # FR-BUILD-05 — one commit for the phase. The turn snapshots stay reachable by label,
        # so a squashed phase is still rewindable.
        build.commit = repo.commit_phase(PHASE, message=f"loom(build): {design.summary}")

    _emit(
        on_event,
        "phase_finished",
        phase=PHASE,
        status=build.status,
        total=None if build.score is None else round(build.score.total, 4),
        turns=build.turns_used,
        usd=round(build.usd_spent, 6),
        snapshot=build.snapshot,
    )
    return build


class _Snapshotter:
    """SEC-08, FR-BUILD-05 — one revert point per turn, taken from the loop's own events.

    The loop emits `turn` at the *start* of a turn, so the snapshot for turn N is taken when
    turn N+1 begins, and the last one when the loop is done. That is one commit per turn with
    honest labels, and it needs nothing from `loop.py` that is not already there.
    """

    def __init__(self, repo: Workspace, forward: Callable[..., Any] | None) -> None:
        self.repo = repo
        self.forward = forward
        self.turn = 0
        self.label = "scaffold"

    def __call__(self, kind: str, **fields: Any) -> None:
        if kind == "turn":
            turn = int(fields.get("turn", 0))
            if turn > 1:
                self._take(turn - 1)
            self.turn = turn
        if self.forward is not None:
            self.forward(kind, **fields)

    def close(self) -> str:
        """Snapshot the final turn and return the label to resume from."""
        if self.turn:
            self._take(self.turn)
        return self.label

    def _take(self, turn: int) -> None:
        self.label = self.repo.snapshot(f"turn-{turn}").label


def _task(design: Design, skeleton: ScaffoldResult, feedback: str = "") -> str:
    """The Design, rendered for the model. It sees the rubric it will be graded against —
    hiding it would only delay the same information until the first failed round, and the
    prompt's integrity clause is what keeps that from being an invitation."""
    lines = [
        f"# What to build\n\n{design.summary}",
        "",
        f"Stack: {', '.join(design.stack) or 'your choice, keep it minimal'}",
        f"Workspace: {skeleton.summary}",
        "",
        "## Files to produce",
    ]
    for spec in design.file_manifest:
        after = f" (after {', '.join(spec.depends_on)})" if spec.depends_on else ""
        lines.append(f"- `{spec.path}`{after} — {spec.purpose}")

    lines += ["", f"## Rubric (threshold {design.rubric.threshold:.2f})"]
    for criterion in design.rubric.criteria:
        hard = " [HARD FAIL]" if criterion.name in design.rubric.hard_fail else ""
        what = criterion.command if criterion.kind == "shell" else criterion.question
        lines.append(
            f"- **{criterion.name}** ({criterion.kind}, weight {criterion.weight:.2f}){hard}: "
            f"{criterion.description or what}"
        )
        if criterion.kind == "shell":
            lines.append(f"    runs: `{criterion.command}`")

    if feedback.strip():
        lines += ["", "## Reviewer feedback on the previous build", "", feedback.strip()]
    return "\n".join(lines)


def _persist_transcript(root: Path, run_id: str, result: LoopResult) -> Path:
    """The build's message history, next to the event stream under `runs/<id>/`. Not an artifact
    — no phase reads it (FR-PIPE-02) — but the on-demand `/compact` (FR-SESS-06) shrinks it here,
    and it is the honest record of what the model actually saw."""
    from loom.session import runs_dir

    directory = runs_dir(root) / (run_id or "latest")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "transcript.json"
    atomic_write(path, json.dumps(result.messages) + "\n")
    return path


def _persist_score(root: Path, run_id: str, score: Score) -> Path:
    """FR-ART-04 — the last grading, inspectable after the fact. Written atomically, after the
    model validated, never before (FR-ART-01)."""
    directory = root / ".loom" / "artifacts" / (run_id or "latest")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "score.json"
    atomic_write(path, score.model_dump_json(indent=2) + "\n")
    return path


def _emit(on_event: Callable[..., Any] | None, kind: str, **fields: Any) -> None:
    if on_event is not None:
        on_event(kind, **fields)
