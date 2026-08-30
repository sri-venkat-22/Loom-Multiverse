from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loom.cli import app, ledger_path
from loom.ledger import Ledger
from loom.session import Session

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No user-level config or LOOM_* env from the dev machine leaking into assertions."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in [k for k in __import__("os").environ if k.startswith("LOOM_")]:
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    return p


def test_init_then_status_exits_zero_and_reports_no_run(project: Path) -> None:
    init = runner.invoke(app, ["init", "-C", str(project)])
    assert init.exit_code == 0, init.output

    status = runner.invoke(app, ["status", "-C", str(project)])
    assert status.exit_code == 0, status.output
    assert "no run in progress" in status.output


def test_init_creates_the_state_dirs_and_ledger(project: Path) -> None:
    runner.invoke(app, ["init", "-C", str(project)])
    d = project / ".loom"
    assert (d / "runs").is_dir() and (d / "artifacts").is_dir()
    assert (d / ".gitignore").read_text() == "*\n"
    assert ledger_path(project).exists()


def test_init_creates_a_git_repo_when_absent(project: Path) -> None:
    result = runner.invoke(app, ["init", "-C", str(project)])
    assert "git repo: created" in result.output
    assert (project / ".git").is_dir()


def test_init_leaves_an_existing_git_repo_alone(project: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    head_before = (project / ".git" / "HEAD").read_text()
    result = runner.invoke(app, ["init", "-C", str(project)])
    assert "git repo: yes" in result.output
    assert (project / ".git" / "HEAD").read_text() == head_before


def test_init_is_idempotent(project: Path) -> None:
    runner.invoke(app, ["init", "-C", str(project)])
    (project / ".loom" / "runs" / "keepme").mkdir()
    second = runner.invoke(app, ["init", "-C", str(project)])
    assert second.exit_code == 0
    assert "already initialised" in second.output
    assert (project / ".loom" / "runs" / "keepme").is_dir()


def test_status_before_init_says_so_without_crashing(project: Path) -> None:
    result = runner.invoke(app, ["status", "-C", str(project)])
    assert result.exit_code == 0
    assert "not initialised" in result.output


def test_status_reports_the_latest_run_and_spend(project: Path) -> None:
    runner.invoke(app, ["init", "-C", str(project)])
    session = Session(project)
    session.log_event("phase_start", phase="validate")
    session.log_event("phase_end", phase="validate")
    Ledger(ledger_path(project)).record(phase="validate", model="qwen", usd=0.1234)

    result = runner.invoke(app, ["status", "-C", str(project)])
    assert result.exit_code == 0
    assert "no run in progress" not in result.output
    assert session.run_id in result.output
    assert "2 events" in result.output
    assert "last event:  phase_end" in result.output
    assert "$0.1234" in result.output


def test_status_reflects_config_precedence(project: Path) -> None:
    runner.invoke(app, ["init", "-C", str(project)])
    (project / ".loom" / "config.toml").write_text('model = "from-project"\n', encoding="utf-8")
    result = runner.invoke(app, ["status", "-C", str(project)])
    assert "from-project" in result.output


def test_status_on_a_broken_config_exits_one_and_points_at_the_file(project: Path) -> None:
    runner.invoke(app, ["init", "-C", str(project)])
    (project / ".loom" / "config.toml").write_text('moddel = "typo"\n', encoding="utf-8")
    result = runner.invoke(app, ["status", "-C", str(project)])
    assert result.exit_code == 1
    assert "config error" in result.output
    assert str(project / ".loom" / "config.toml") in result.output
