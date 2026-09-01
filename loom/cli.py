"""The terminal front end. The only module allowed to import typer or to print."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Callable
from enum import IntEnum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

if TYPE_CHECKING:
    from loom.tui.commands import RunState

from loom.agent.providers import (
    LiteLLMProvider,
    ProviderError,
    ProviderQuotaError,
    key_variable_for,
)
from loom.agent.tools.ask_user import AskUser
from loom.blueprints.loader import load_blueprint
from loom.cache import PhaseCache
from loom.config import (
    Config,
    apply_credentials,
    load_config,
    project_config_path,
    user_config_path,
)
from loom.contracts import Provider
from loom.diagnostics import bug_bundle, format_report
from loom.diagnostics import doctor as run_doctor
from loom.gates import AutoApprove, TerminalGate
from loom.ledger import Ledger
from loom.pipeline import PHASES, PipelineResult, UnattendedWithoutBudget, run_pipeline
from loom.replay import replay as replay_phase
from loom.session import LOOM_DIR, Session, list_runs
from loom.tui.app import start_session
from loom.tui.banner import BannerState
from loom.tui.theme import DEFAULT_THEME
from loom.ui import TokenStream, event_sink

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
BlueprintOpt = typer.Option(
    None, "--blueprint", help="Blueprint path or first-party name to bias the design."
)
JsonOpt = typer.Option(False, "--json", help="Emit events as JSON Lines; no other output.")
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


def initialise(root: Path) -> tuple[bool, bool]:
    """Create `.loom/{runs,artifacts,cache}`, the gitignore and the ledger, and `git init` if
    the directory is not already a repo (FR-CLI-04). Returns (was_fresh, git_created).

    Shared by the `init` command and the session's first-run offer (FR-CLI-05) so the two
    cannot drift on what "initialised" means.
    """
    root.mkdir(parents=True, exist_ok=True)
    d = loom_dir(root)
    fresh = not d.exists()
    for sub in (d, d / "runs", d / "artifacts", d / "cache"):
        sub.mkdir(parents=True, exist_ok=True)
    # Loom's own state is never the user's to commit.
    (d / ".gitignore").write_text("*\n", encoding="utf-8")
    Ledger(ledger_path(root))

    git_created = not _is_git_repo(root)
    if git_created:
        subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    return fresh, git_created


def _init_and_report(root: Path) -> None:
    fresh, git_created = initialise(root)
    typer.echo(f"{'initialised' if fresh else 'already initialised'} {loom_dir(root)}")
    typer.echo("git repo: created" if git_created else "git repo: yes")


@app.command()
def init(path: Path = PathOpt) -> None:
    """Create `.loom/` and make sure the project is a git repo."""
    _init_and_report(path.resolve())


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


# ----------------------------------------------------------------- the session shell (8.1)

#: Context windows for the models Loom names itself, curated for the same reason
#: `config.DEFAULT_PRICE_TABLE` is: `litellm.get_model_info` is unreliable for these
#: openrouter/anthropic strings. A model not listed here shows no context (honest over guessed).
_CONTEXT_TOKENS: dict[str, int] = {
    "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b": 1_000_000,
    "openrouter/qwen/qwen3-coder": 262_144,
    "openrouter/qwen/qwen3-coder-480b": 262_144,
    "anthropic/claude-sonnet-5": 200_000,
}


def _version() -> str:
    try:
        return _pkg_version("loom-cli")
    except PackageNotFoundError:  # pragma: no cover - only when running from an unbuilt tree
        return "0+unknown"


def _display_cwd(root: Path) -> str:
    home = Path.home()
    if root == home:
        return "~"
    try:
        return f"~/{root.relative_to(home)}"
    except ValueError:
        return str(root)


def _credential_source(model: str, set_from_file: list[str]) -> str:
    """Where the provider key came from — never the key itself (FR-CLI-03)."""
    var = key_variable_for(model)
    if not var:
        return "local model — no key needed"
    if var in set_from_file:
        return "~/.loom/credentials.json"
    if os.environ.get(var):
        return f"{var} (env)"
    return f"not set — export {var}"


def _run_state(root: Path) -> str:
    if not loom_dir(root).exists():
        return "no run in progress"
    session = Session.latest(root)
    if session is None:
        return "no run in progress · / for commands"
    spent = Ledger(ledger_path(root)).total(session.run_id)
    return f"run {session.run_id} · ${spent:.2f} spent"


def _banner_state(root: Path) -> BannerState:
    config = load_config(cwd=root)
    set_from_file = apply_credentials()  # FR-CFG-06 — keys into env, so the source is knowable
    return BannerState(
        version=_version(),
        model=config.model.split("/")[-1].split(":")[0],
        context_tokens=_CONTEXT_TOKENS.get(config.model),
        effort=config.effort,
        billing="BYOK",  # the only R1 mode; R2 shows the account plan (FR-ACCT-04)
        provider=config.model.split("/")[0],
        cwd=_display_cwd(root),
        credential_source=_credential_source(config.model, set_from_file),
        run_state=_run_state(root),
        mode=config.mode,
        theme=DEFAULT_THEME,
    )


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context, path: Path = PathOpt) -> None:
    """`loom` with no subcommand starts the interactive session (FR-CLI-01)."""
    if ctx.invoked_subcommand is not None:
        return
    root = path.resolve()
    try:
        state = _banner_state(root)
    except Exception as exc:  # a bad config or credentials file should not crash into a traceback
        typer.echo(f"cannot start a session: {exc}")
        raise typer.Exit(ExitCode.USAGE) from exc
    code = start_session(
        state=state,
        initialised=loom_dir(root).exists(),
        is_tty=sys.stdin.isatty(),
        out=sys.stdout,
        ask=input,
        on_init=lambda: _init_and_report(root),
        repl=_repl_for(root) if loom_dir(root).exists() else None,
    )
    raise typer.Exit(code)


def _repl_for(root: Path) -> Callable[[Any], int]:
    """Build the WP-8.2 REPL, wired to the pipeline the flags already reach (the `loom/tui/`
    invariant: every path it drives is also reachable non-interactively)."""
    from loom.tui.repl import ReplActions, make_repl

    config = load_config(cwd=root)

    def run(start: str, stop: str, idea: str, run_id: str | None) -> None:
        # The same path `loom run`/`loom build` take, wrapped so a finished (or refused) phase
        # returns to the prompt instead of exiting the process (FR-REPL-01).
        try:
            _run_phases(
                idea=idea,
                path=root,
                start=start,
                stop=stop,
                model=None,
                judge_model=None,
                effort=None,
                budget=None,
                max_turns=None,
                max_usd=None,
                yes=False,
                no_cache=False,
                run_id=run_id,
            )
        except typer.Exit as exc:
            if int(exc.exit_code or 0) != 0:
                typer.echo(f"(phase ended with exit {int(exc.exit_code or 0)})")

    def do_replay(phase: str) -> None:
        """`/replay` from the REPL — the same `replay_phase` path `loom replay` takes, against
        the latest run's cached upstream artifacts, printing the diff and returning to the prompt.
        """
        rid = _latest_run(root)
        if rid is None:
            typer.echo("no run to replay")
            return
        apply_credentials()
        cfg = load_config(cwd=root)
        session = Session(root, rid)
        provider = LiteLLMProvider(
            cfg.model,
            price_table=cfg.price_table,
            ledger=Ledger(ledger_path(root)),
            phase=phase,
            run_id=rid,
            on_event=session.log_event,
        )
        try:
            result = asyncio.run(
                replay_phase(
                    root=root,
                    run_id=rid,
                    phase=phase,
                    provider=provider,
                    config=cfg,
                    prompt_path=None,
                    session=session,
                    ask_user_fn=AskUser(yes=True, on_event=session.log_event),
                )
            )
        except (ProviderError, ValueError, FileNotFoundError) as exc:
            typer.echo(str(exc))
            return
        typer.echo(result.diff)

    actions = ReplActions(
        start_idea=lambda idea: run("validate", "build", idea, None),
        start_run=lambda s, st, _rf: run(s, st, "", _latest_run(root)),
        resume=lambda rid: run(_resume_start(root, rid), "build", "", rid),
        replay=do_replay,
        run_state=lambda: _repl_run_state(root),
        cost_report=lambda: _cost_string(root),
        status_report=lambda: _status_string(root, load_config(cwd=root)),
    )
    return make_repl(root=root, config=config, actions=actions, theme=DEFAULT_THEME)


def _repl_run_state(root: Path) -> RunState:
    """The between-prompts run state (FR-REPL-03). Synchronously the REPL only rests at `idle` or
    `finished`; `gate`/`running` are reached once a phase runs in the background (WP-8.5)."""
    from loom.phases.base import artifact_path

    session = Session.latest(root)
    if session is None:
        return "idle"
    done = all(artifact_path(root, session.run_id, p).is_file() for p in PHASES)
    return "finished" if done else "idle"


def _resume_start(root: Path, run_id: str) -> str:
    from loom.phases.base import artifact_path

    todo = [p for p in PHASES if not artifact_path(root, run_id, p).is_file()]
    return todo[0] if todo else "build"


def _cost_string(root: Path) -> str:
    ledger = Ledger(ledger_path(root))
    lines = [f"total: ${ledger.total():.4f}"]
    for phase, usd in ledger.by_phase().items():
        lines.append(f"  {phase:<10} ${usd:.4f}")
    return "\n".join(lines)


def _status_string(root: Path, config: Config) -> str:
    return (
        f"model {config.model} · effort {config.effort} · mode {config.mode} · "
        f"${config.budget_usd:.2f} budget"
    )


# --------------------------------------------------------------------------- the pipeline


def _providers(
    config: Config,
    *,
    root: Path,
    session: Session,
    ledger: Ledger,
    on_token: Callable[[str], None] | None = None,
) -> tuple[Any, Provider]:
    """A per-phase provider factory, and the judge.

    One provider per phase rather than one per run, so that `loom cost` can say which phase
    spent the money. The judge is built without a ledger of its own — `rubric.grade()` records
    its spend under phase "judge", and a second ledger would double-count it.

    `on_token`, when set (an interactive TTY run), makes each phase provider stream and forward
    assistant text as it arrives (FR-REPL-05). The judge never streams — a rubric grade is not
    something a human watches token by token.
    """

    def factory(phase: str) -> Provider:
        return LiteLLMProvider(
            config.model,
            price_table=config.price_table,
            ledger=ledger,
            phase=phase,
            run_id=session.run_id,
            on_event=session.log_event,
            on_token=on_token,
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
    blueprint: str | None = None,
    run_id: str | None = None,
    json_out: bool = False,
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
                "blueprint": blueprint,
            },
        )
    except Exception as exc:
        typer.echo(f"config error: {exc}")
        raise typer.Exit(ExitCode.USAGE) from exc

    if config.blueprint:
        # Fail fast on a bad --blueprint before validate and plan spend a cent reaching design.
        try:
            load_blueprint(config.blueprint)
        except (OSError, ValueError) as exc:
            typer.echo(f"blueprint error: {exc}")
            raise typer.Exit(ExitCode.USAGE) from exc

    session = Session(root, run_id)
    # FR-HEADLESS-02/03 — render the same events `session.log_event` writes to disk. Tapping the
    # instance's method is the one hook every event routes through: the pipeline, the phases and
    # the loop all narrate through `session.log_event`, so wrapping it here catches them all.
    sink = event_sink(sys.stdout, json_mode=json_out, tty=sys.stdout.isatty())
    if sink is not None:
        _log = session.log_event

        def _log_and_render(kind: str, **fields: Any) -> dict[str, Any]:
            event = _log(kind, **fields)
            sink(event)
            return event

        session.log_event = _log_and_render  # type: ignore[method-assign]

    ledger = Ledger(ledger_path(root))
    # FR-REPL-05 — stream assistant text only on an interactive TTY. A piped or `--json` run keeps
    # its one-line-per-turn output (FR-HEADLESS-02/03); tokens rewriting a log would be noise.
    stream_tokens = TokenStream(sys.stdout) if (not json_out and sys.stdout.isatty()) else None
    factory, judge = _providers(
        config, root=root, session=session, ledger=ledger, on_token=stream_tokens
    )
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
    except ProviderQuotaError as exc:
        # Exit 6, not 2: nothing about the invocation was wrong, and a script that retries on
        # a usage error would hammer a limit that only a clock will clear.
        typer.echo(str(exc))
        raise typer.Exit(ExitCode.QUOTA) from exc
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

    if not json_out:  # FR-HEADLESS-03 — JSON Lines is the whole of stdout, nothing else.
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
    blueprint: str = BlueprintOpt,
    json_out: bool = JsonOpt,
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
        blueprint=blueprint,
        json_out=json_out,
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
    blueprint: str = BlueprintOpt,
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
        blueprint=blueprint,
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
    json_out: bool = JsonOpt,
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
        json_out=json_out,
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
    except ProviderQuotaError as exc:
        typer.echo(str(exc))
        raise typer.Exit(ExitCode.QUOTA) from exc
    except (ValueError, FileNotFoundError, ProviderError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc

    typer.echo(result.diff)
    typer.echo(f"\n{result.previous_path.name} -> {result.path.name}  (${result.usd_spent:.4f})")


@app.command()
def preview(
    path: Path = PathOpt,
    timeout: float = typer.Option(30.0, "--timeout", help="Seconds to wait for the app to answer."),
) -> None:
    """Boot the built app on a local URL and hold it until Ctrl-C. FR-BP-03."""
    import time

    from loom.preview import PreviewError
    from loom.preview import preview as boot_preview

    root = path.resolve()
    try:
        with boot_preview(root, timeout=timeout) as live:
            typer.echo(f"serving {live.target} at {live.url}  (Ctrl-C to stop)")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
    except PreviewError as exc:
        typer.echo(str(exc))
        raise typer.Exit(ExitCode.USAGE) from exc
    typer.echo("\nstopped; port released")


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


@app.command()
def doctor(path: Path = PathOpt) -> None:
    """Check Python, git, the provider key, reachability, workspace and disk (FR-DIAG-02)."""
    root = path.resolve()
    apply_credentials()  # so the credential check sees keys from .env / credentials.json
    checks = run_doctor(root)
    typer.echo(format_report(checks, ok="✓", fail="✗"))
    if not all(c.ok for c in checks):
        raise typer.Exit(ExitCode.ERROR)


@app.command()
def bug(path: Path = PathOpt) -> None:
    """Write a redacted diagnostic bundle to a file. Nothing is uploaded (FR-DIAG-03)."""
    root = path.resolve()
    apply_credentials()
    dest = bug_bundle(root)
    typer.echo(f"wrote a redacted diagnostic bundle → {dest}")
    typer.echo("secrets are stripped and nothing was uploaded; attach it to a bug report.")


def main() -> None:  # pragma: no cover - console-script entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
