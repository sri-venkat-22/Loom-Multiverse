"""The terminal front end. The only module allowed to import typer or to print."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from enum import IntEnum
from pathlib import Path
from typing import Any

import typer

from loom.agent.providers import LiteLLMProvider, ProviderError
from loom.agent.tools.ask_user import AskUser
from loom.cache import PhaseCache
from loom.config import (
    Config,
    apply_credentials,
    load_config,
    project_config_path,
    user_config_path,
)
from loom.contracts import Provider
from loom.gates import AutoApprove, TerminalGate
from loom.ledger import Ledger
from loom.pipeline import PHASES, PipelineResult, UnattendedWithoutBudget, run_pipeline
from loom.replay import replay as replay_phase
from loom.session import LOOM_DIR, Session, list_runs

app = typer.Typer(add_completion=False, help="Turn an idea into a working, tested codebase.")


class ExitCode(IntEnum):
    """FR-CLI-06 — stable across releases. Never renumber one."""

    OK = 0
    ERROR = 1  # unexpected
    USAGE = 2  # the user asked for something impossible
    BUDGET = 3  # budget exhausted
    RUBRIC = 4  # finished, but under threshold
    INTERRUPTED = 5
    QUOTA = 6  # R2, account quota exceeded


#: FR-CLI-06 — how a finished run becomes an exit status. One place, so `loom run` and
#: `loom build` cannot disagree about what "stalled" is worth.
EXIT_FOR_STATUS: dict[str, ExitCode] = {
    "passed": ExitCode.OK,
    "abandoned": ExitCode.OK,
    "no-go": ExitCode.OK,
    "budget_exhausted": ExitCode.BUDGET,
    "stalled": ExitCode.RUBRIC,
    "blocked": ExitCode.RUBRIC,
    "invalid": ExitCode.ERROR,
}

PathOpt = typer.Option(Path("."), "--path", "-C", help="Project directory.")
ModelOpt = typer.Option(None, "--model", help="Override the model for this run.")
JudgeOpt = typer.Option(None, "--judge-model", help="Override the rubric judge model.")
EffortOpt = typer.Option(None, "--effort", help="low | medium | high | xhigh | max.")
BudgetOpt = typer.Option(None, "--budget", help="USD ceiling for the whole run.")
MaxTurnsOpt = typer.Option(None, "--max-turns", help="Turn cap per phase.")
MaxUsdOpt = typer.Option(None, "--max-usd", help="USD cap per phase.")
YesOpt = typer.Option(False, "--yes", "-y", help="Auto-approve every gate. No TTY needed.")
NoCacheOpt = typer.Option(False, "--no-cache", help="Ignore the phase cache.")
RunOpt = typer.Option(None, "--run", help="Which run. Default: the latest.")
PromptOpt = typer.Option(None, "--prompt", help="Use this prompt file instead.")


def loom_dir(root: Path) -> Path:
    return root / LOOM_DIR


def ledger_path(root: Path) -> Path:
    return loom_dir(root) / "ledger.db"


def _is_git_repo(root: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@app.command()
def init(path: Path = PathOpt) -> None:
    """Create `.loom/` and make sure the project is a git repo."""
    root = path.resolve()
    root.mkdir(parents=True, exist_ok=True)

    d = loom_dir(root)
    fresh = not d.exists()
    for sub in (d, d / "runs", d / "artifacts", d / "cache"):  # FR-CLI-04
        sub.mkdir(parents=True, exist_ok=True)
    # Loom's own state is never the user's to commit.
    (d / ".gitignore").write_text("*\n", encoding="utf-8")
    Ledger(ledger_path(root))

    typer.echo(f"{'initialised' if fresh else 'already initialised'} {d}")

    if _is_git_repo(root):
        typer.echo("git repo: yes")
    else:
        subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
        typer.echo("git repo: created")


@app.command()
def status(path: Path = PathOpt) -> None:
    """Print config in effect, the latest run, and spend to date."""
    root = path.resolve()
    if not loom_dir(root).exists():
        typer.echo(f"not initialised — run `loom init -C {root}`")
        raise typer.Exit(ExitCode.OK)

    try:
        cfg = load_config(cwd=root)
    except Exception as exc:  # a bad config file should still let you see where it is
        typer.echo(f"config error: {exc}")
        typer.echo(f"  project: {project_config_path(root)}")
        typer.echo(f"  user:    {user_config_path(Path.home())}")
        raise typer.Exit(ExitCode.USAGE) from exc

    typer.echo(f"project:     {root}")
    typer.echo(f"model:       {cfg.model}")
    typer.echo(f"judge model: {cfg.judge_model}")
    typer.echo(f"effort:      {cfg.effort} ({cfg.max_turns} turns max), mode {cfg.mode}")
    typer.echo(f"budget:      ${cfg.budget_usd:.2f} per run, ${cfg.max_usd:.2f} per phase")

    runs = list_runs(root)
    session = Session.latest(root)
    if session is None:
        typer.echo("no run in progress")
    else:
        events = session.read_events()
        last = events[-1]["kind"] if events else "no events"
        typer.echo(f"runs:        {len(runs)} (latest {session.run_id}, {len(events)} events)")
        typer.echo(f"last event:  {last}")

    ledger = Ledger(ledger_path(root))
    by_phase = ledger.by_phase()
    typer.echo(f"spend:       ${ledger.total():.4f} total")
    for phase, usd in by_phase.items():
        typer.echo(f"  {phase:<10} ${usd:.4f}")


# --------------------------------------------------------------------------- the pipeline


def _providers(
    config: Config, *, root: Path, session: Session, ledger: Ledger
) -> tuple[Any, Provider]:
    """A per-phase provider factory, and the judge.

    One provider per phase rather than one per run, so that `loom cost` can say which phase
    spent the money. The judge is built without a ledger of its own — `rubric.grade()` records
    its spend under phase "judge", and a second ledger would double-count it.
    """

    def factory(phase: str) -> Provider:
        return LiteLLMProvider(
            config.model,
            price_table=config.price_table,
            ledger=ledger,
            phase=phase,
            run_id=session.run_id,
            on_event=session.log_event,
        )

    judge = LiteLLMProvider(config.judge_model, price_table=config.price_table)
    return factory, judge


def _run_phases(
    *,
    idea: str,
    path: Path,
    start: str,
    stop: str,
    model: str | None,
    judge_model: str | None,
    effort: str | None,
    budget: float | None,
    max_turns: int | None,
    max_usd: float | None,
    yes: bool,
    no_cache: bool,
    run_id: str | None = None,
) -> PipelineResult:
    """Everything `loom run` and the four single-phase commands share."""
    root = path.resolve()
    if not loom_dir(root).exists():
        typer.echo(f"not initialised — run `loom init -C {root}`")
        raise typer.Exit(ExitCode.USAGE)

    try:
        apply_credentials()  # FR-CFG-06
        config = load_config(
            cwd=root,
            flags={
                "model": model,
                "judge_model": judge_model,
                "effort": effort,
                "max_turns": max_turns,
                "max_usd": max_usd,
                "budget_usd": budget,
            },
        )
    except Exception as exc:
        typer.echo(f"config error: {exc}")
        raise typer.Exit(ExitCode.USAGE) from exc

    session = Session(root, run_id)
    ledger = Ledger(ledger_path(root))
    factory, judge = _providers(config, root=root, session=session, ledger=ledger)
    unattended = config.mode == "unattended"
    # FR-HEADLESS-01 — `--yes` is the whole non-interactive path, not a second one. The gate is
    # the only thing that differs, and both write the same events.
    if not (yes or unattended) and not sys.stdin.isatty():
        # Auto-approving would be the friendlier default and the wrong one: nobody piping into
        # a command expects it to spend money unsupervised because it could not find them.
        typer.echo(
            "there is no terminal to approve at, and gates need one. Pass --yes to approve "
            "every gate automatically, or run this from a terminal."
        )
        raise typer.Exit(ExitCode.USAGE)
    gate = (
        AutoApprove(on_event=session.log_event)
        if (yes or unattended)
        else TerminalGate(on_event=session.log_event)
    )

    try:
        with session.interruptible(phase=start):
            result = asyncio.run(
                run_pipeline(
                    idea,
                    root=root,
                    provider_factory=factory,
                    config=config,
                    judge_provider=judge,
                    session=session,
                    gate=gate,
                    start=start,
                    stop=stop,
                    cache=PhaseCache(root, enabled=not no_cache),
                    ask_user_fn=AskUser(yes=yes or unattended, on_event=session.log_event),
                    unattended=unattended,
                    budget_usd=budget,
                )
            )
    except UnattendedWithoutBudget as exc:
        typer.echo(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc
    except ProviderError as exc:
        # The first wall every new user hits. A traceback here reads as "this tool is broken".
        typer.echo(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc
    except ValueError as exc:  # an unknown phase name, or an upstream artifact that is missing
        typer.echo(f"cannot run: {exc}")
        raise typer.Exit(ExitCode.USAGE) from exc
    except FileNotFoundError as exc:
        typer.echo(f"{exc}")
        raise typer.Exit(ExitCode.USAGE) from exc
    except KeyboardInterrupt:
        typer.echo("interrupted — the run is on disk and resumable with `loom resume`")
        raise typer.Exit(ExitCode.INTERRUPTED) from None

    _report(result)
    code = EXIT_FOR_STATUS.get(result.status, ExitCode.ERROR)
    if code != ExitCode.OK:
        raise typer.Exit(code)
    return result


def _report(result: PipelineResult) -> None:
    typer.echo(f"\nrun {result.run_id}: {result.status} (${result.usd_spent:.4f})")
    for phase, where in result.artifacts.items():
        typer.echo(f"  {phase:<9} {where}")
    if result.build is not None and result.build.score is not None:
        typer.echo(f"  rubric    {result.build.score.total:.2f}")
    if result.message:
        typer.echo(f"\n{result.message}")


@app.command()
def run(
    idea: str = typer.Argument(..., help="The idea, in a sentence."),
    path: Path = PathOpt,
    from_: str = typer.Option("validate", "--from", help=f"Start at: {', '.join(PHASES)}."),
    to: str = typer.Option("build", "--to", help="Stop after this phase."),
    model: str = ModelOpt,
    judge_model: str = JudgeOpt,
    effort: str = EffortOpt,
    budget: float = BudgetOpt,
    max_turns: int = MaxTurnsOpt,
    max_usd: float = MaxUsdOpt,
    yes: bool = YesOpt,
    no_cache: bool = NoCacheOpt,
) -> None:
    """Validate, plan, design and build, with a gate between each."""
    _run_phases(
        idea=idea,
        path=path,
        start=from_,
        stop=to,
        model=model,
        judge_model=judge_model,
        effort=effort,
        budget=budget,
        max_turns=max_turns,
        max_usd=max_usd,
        yes=yes,
        no_cache=no_cache,
    )


@app.command()
def validate(
    idea: str = typer.Argument(..., help="The idea, in a sentence."),
    path: Path = PathOpt,
    model: str = ModelOpt,
    effort: str = EffortOpt,
    budget: float = BudgetOpt,
    yes: bool = YesOpt,
    no_cache: bool = NoCacheOpt,
) -> None:
    """Research the idea and produce `validation.json`."""
    _run_phases(
        idea=idea,
        path=path,
        start="validate",
        stop="validate",
        model=model,
        judge_model=None,
        effort=effort,
        budget=budget,
        max_turns=None,
        max_usd=None,
        yes=yes,
        no_cache=no_cache,
    )


def _continue(phase: str, path: Path, run_id: str | None, **flags: Any) -> None:
    """FR-PIPE-04 — one phase, against whatever the run already has on disk."""
    root = path.resolve()
    resolved = run_id or _latest_run(root)
    if resolved is None:
        typer.echo('no run to continue — start one with `loom run "<idea>"`')
        raise typer.Exit(ExitCode.USAGE)
    _run_phases(idea="", path=path, start=phase, stop=phase, run_id=resolved, **flags)


def _latest_run(root: Path) -> str | None:
    runs = list_runs(root)
    return runs[-1] if runs else None


@app.command()
def plan(
    path: Path = PathOpt,
    run_id: str = RunOpt,
    model: str = ModelOpt,
    effort: str = EffortOpt,
    budget: float = BudgetOpt,
    yes: bool = YesOpt,
    no_cache: bool = NoCacheOpt,
) -> None:
    """Turn the run's `validation.json` into `prd.json`."""
    _continue(
        "plan",
        path,
        run_id,
        model=model,
        judge_model=None,
        effort=effort,
        budget=budget,
        max_turns=None,
        max_usd=None,
        yes=yes,
        no_cache=no_cache,
    )


@app.command()
def design(
    path: Path = PathOpt,
    run_id: str = RunOpt,
    model: str = ModelOpt,
    effort: str = EffortOpt,
    budget: float = BudgetOpt,
    yes: bool = YesOpt,
    no_cache: bool = NoCacheOpt,
) -> None:
    """Turn the run's `prd.json` into `design.json`, rubric included."""
    _continue(
        "design",
        path,
        run_id,
        model=model,
        judge_model=None,
        effort=effort,
        budget=budget,
        max_turns=None,
        max_usd=None,
        yes=yes,
        no_cache=no_cache,
    )


@app.command()
def build(
    path: Path = PathOpt,
    run_id: str = RunOpt,
    model: str = ModelOpt,
    judge_model: str = JudgeOpt,
    effort: str = EffortOpt,
    budget: float = BudgetOpt,
    max_turns: int = MaxTurnsOpt,
    max_usd: float = MaxUsdOpt,
    yes: bool = YesOpt,
    no_cache: bool = NoCacheOpt,
) -> None:
    """Scaffold, write the code, and grade it until the rubric passes."""
    _continue(
        "build",
        path,
        run_id,
        model=model,
        judge_model=judge_model,
        effort=effort,
        budget=budget,
        max_turns=max_turns,
        max_usd=max_usd,
        yes=yes,
        no_cache=no_cache,
    )


@app.command()
def resume(
    run_id: str = typer.Argument(None, help="Which run. Default: the latest."),
    path: Path = PathOpt,
    budget: float = BudgetOpt,
    yes: bool = YesOpt,
) -> None:
    """Continue a run from the first phase whose artifact is missing."""
    from loom.phases.base import artifact_path

    root = path.resolve()
    resolved = run_id or _latest_run(root)
    if resolved is None:
        typer.echo("no run to resume")
        raise typer.Exit(ExitCode.USAGE)

    todo = [p for p in PHASES if not artifact_path(root, resolved, p).is_file()]
    if not todo:
        typer.echo(f"run {resolved} has every artifact; nothing to resume")
        raise typer.Exit(ExitCode.OK)

    typer.echo(f"resuming {resolved} at {todo[0]}")
    _run_phases(
        idea="",
        path=path,
        start=todo[0],
        stop="build",
        model=None,
        judge_model=None,
        effort=None,
        budget=budget,
        max_turns=None,
        max_usd=None,
        yes=yes,
        no_cache=False,
        run_id=resolved,
    )


@app.command()
def replay(
    run_id: str = typer.Argument(..., help="The run to replay a phase of."),
    phase: str = typer.Option(..., "--phase", help="validate, plan or design."),
    path: Path = PathOpt,
    prompt: Path = PromptOpt,
    model: str = ModelOpt,
) -> None:
    """Re-run one phase against a run's cached artifacts, and diff the result."""
    root = path.resolve()
    apply_credentials()
    config = load_config(cwd=root, flags={"model": model})
    session = Session(root, run_id)
    ledger = Ledger(ledger_path(root))
    provider = LiteLLMProvider(
        config.model,
        price_table=config.price_table,
        ledger=ledger,
        phase=phase,
        run_id=run_id,
        on_event=session.log_event,
    )
    try:
        result = asyncio.run(
            replay_phase(
                root=root,
                run_id=run_id,
                phase=phase,
                provider=provider,
                config=config,
                prompt_path=prompt,
                session=session,
                ask_user_fn=AskUser(yes=True, on_event=session.log_event),
            )
        )
    except (ValueError, FileNotFoundError, ProviderError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc

    typer.echo(result.diff)
    typer.echo(f"\n{result.previous_path.name} -> {result.path.name}  (${result.usd_spent:.4f})")


@app.command()
def cost(
    path: Path = PathOpt,
    run_id: str = RunOpt,
) -> None:
    """Spend by phase and by model."""
    root = path.resolve()
    ledger = Ledger(ledger_path(root))
    scope = run_id or None
    typer.echo(f"total: ${ledger.total(scope):.4f}" + (f"  (run {scope})" if scope else ""))
    for label, rows in (("phase", ledger.by_phase(scope)), ("model", ledger.by_model(scope))):
        if rows:
            typer.echo(f"\nby {label}:")
            for name, usd in rows.items():
                typer.echo(f"  {name:<28} ${usd:.4f}")


def main() -> None:  # pragma: no cover - console-script entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
