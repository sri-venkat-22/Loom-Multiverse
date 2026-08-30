"""The terminal front end. The only module allowed to import typer or to print."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from loom.config import load_config, project_config_path, user_config_path
from loom.ledger import Ledger
from loom.session import LOOM_DIR, Session, list_runs

app = typer.Typer(add_completion=False, help="Turn an idea into a working, tested codebase.")

PathOpt = typer.Option(Path("."), "--path", "-C", help="Project directory.")


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
    for sub in (d, d / "runs", d / "artifacts"):
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
        raise typer.Exit(0)

    try:
        cfg = load_config(cwd=root)
    except Exception as exc:  # a bad config file should still let you see where it is
        typer.echo(f"config error: {exc}")
        typer.echo(f"  project: {project_config_path(root)}")
        typer.echo(f"  user:    {user_config_path(Path.home())}")
        raise typer.Exit(1) from exc

    typer.echo(f"project:     {root}")
    typer.echo(f"model:       {cfg.model}")
    typer.echo(f"judge model: {cfg.judge_model}")
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


def main() -> None:  # pragma: no cover - console-script entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
