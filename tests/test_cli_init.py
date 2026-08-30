"""FR-CLI-01, FR-CLI-04, FR-CLI-06, FR-CFG-03, FR-DIAG-01, FR-WS-04."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loom.cli import ExitCode, app, ledger_path
from loom.ledger import Ledger
from loom.session import Session

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No user-level config or LOOM_* env from the dev machine leaking into assertions."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in [k for k in os.environ if k.startswith("LOOM_")]:
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    return p


# --------------------------------------------------------------------------- init


def test_init_then_status_exits_zero_and_reports_no_run(project: Path) -> None:
    """FR-DIAG-01."""
    init = runner.invoke(app, ["init", "-C", str(project)])
    assert init.exit_code == ExitCode.OK, init.output

    status = runner.invoke(app, ["status", "-C", str(project)])
    assert status.exit_code == ExitCode.OK, status.output
    assert "no run in progress" in status.output


def test_init_creates_every_state_dir_and_the_ledger(project: Path) -> None:
    """FR-CLI-04 — runs, artifacts and cache, plus the ledger."""
    runner.invoke(app, ["init", "-C", str(project)])
    d = project / ".loom"
    for sub in ("runs", "artifacts", "cache"):
        assert (d / sub).is_dir(), sub
    assert ledger_path(project).exists()


def test_init_gitignores_loom_state(project: Path) -> None:
    """FR-WS-04 — Loom's state never lands in the user's commits."""
    runner.invoke(app, ["init", "-C", str(project)])
    assert (project / ".loom" / ".gitignore").read_text() == "*\n"


def test_init_creates_a_git_repo_when_absent(project: Path) -> None:
    """FR-CLI-04."""
    result = runner.invoke(app, ["init", "-C", str(project)])
    assert "git repo: created" in result.output
    assert (project / ".git").is_dir()


def test_init_leaves_an_existing_git_repo_alone(project: Path) -> None:
    """FR-CLI-04."""
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    head_before = (project / ".git" / "HEAD").read_text()
    result = runner.invoke(app, ["init", "-C", str(project)])
    assert "git repo: yes" in result.output
    assert (project / ".git" / "HEAD").read_text() == head_before


def test_init_is_idempotent(project: Path) -> None:
    runner.invoke(app, ["init", "-C", str(project)])
    (project / ".loom" / "runs" / "keepme").mkdir()
    second = runner.invoke(app, ["init", "-C", str(project)])
    assert second.exit_code == ExitCode.OK
    assert "already initialised" in second.output
    assert (project / ".loom" / "runs" / "keepme").is_dir()


# --------------------------------------------------------------------------- status


def test_status_before_init_says_so_without_crashing(project: Path) -> None:
    result = runner.invoke(app, ["status", "-C", str(project)])
    assert result.exit_code == ExitCode.OK
    assert "not initialised" in result.output


def test_status_prints_every_field_the_srs_asks_for(project: Path) -> None:
    """FR-DIAG-01 — path, model, judge model, budgets, runs, latest run, last event, spend."""
    runner.invoke(app, ["init", "-C", str(project)])
    session = Session(project)
    session.log_event("phase_started", phase="validate")
    session.log_event("phase_finished", phase="validate")
    Ledger(ledger_path(project)).record(phase="validate", model="qwen", usd=0.1234)

    result = runner.invoke(app, ["status", "-C", str(project)])
    assert result.exit_code == ExitCode.OK
    assert str(project) in result.output
    assert "openrouter/qwen/qwen3-coder" in result.output  # model and judge model
    assert "effort:      medium" in result.output
    assert "mode auto" in result.output
    assert "$5.00 per run" in result.output and "$1.00 per phase" in result.output
    assert "no run in progress" not in result.output
    assert session.run_id in result.output
    assert "2 events" in result.output
    assert "last event:  phase_finished" in result.output
    assert "$0.1234" in result.output
    assert "validate" in result.output  # spend by phase


def test_status_reflects_config_precedence(project: Path) -> None:
    """FR-CFG-01."""
    runner.invoke(app, ["init", "-C", str(project)])
    (project / ".loom" / "config.toml").write_text(
        'model = "from-project"\neffort = "max"\n', encoding="utf-8"
    )
    result = runner.invoke(app, ["status", "-C", str(project)])
    assert "from-project" in result.output
    assert "effort:      max (100 turns max)" in result.output


def test_status_on_a_broken_config_still_runs_and_names_both_paths(project: Path) -> None:
    """FR-CFG-03 — and FR-CLI-06: a bad config is usage, not an internal error."""
    runner.invoke(app, ["init", "-C", str(project)])
    (project / ".loom" / "config.toml").write_text('moddel = "typo"\n', encoding="utf-8")
    result = runner.invoke(app, ["status", "-C", str(project)])
    assert result.exit_code == ExitCode.USAGE == 2
    assert "config error" in result.output
    assert str(project / ".loom" / "config.toml") in result.output
    assert "config.toml" in result.output.splitlines()[-1]  # the user-level path too


# --------------------------------------------------------------------------- surface


def test_help_lists_every_implemented_subcommand(project: Path) -> None:
    """FR-CLI-01 — every subcommand is runnable directly, and discoverable."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == ExitCode.OK
    for command in ("init", "status"):
        assert command in result.output


def test_exit_codes_are_the_documented_ones() -> None:
    """FR-CLI-06 — stable across releases; never renumber one."""
    assert [(c.name, int(c)) for c in ExitCode] == [
        ("OK", 0),
        ("ERROR", 1),
        ("USAGE", 2),
        ("BUDGET", 3),
        ("RUBRIC", 4),
        ("INTERRUPTED", 5),
        ("QUOTA", 6),
    ]


def test_an_unknown_subcommand_is_a_usage_error(project: Path) -> None:
    """FR-CLI-06 — code 2 is bad usage."""
    result = runner.invoke(app, ["frobnicate"])
    assert result.exit_code == ExitCode.USAGE
