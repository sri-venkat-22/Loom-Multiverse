"""WP-3.6/3.7 at the command line. FR-PIPE-03/04, FR-HEADLESS-01/04, FR-CLI-06, FR-SESS-04/05.

The provider is monkeypatched at the one seam it enters the CLI through, so every command here
runs end to end — argument parsing, config precedence, session, gate, artifacts, exit code —
with no network and no key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import loom.cli as cli
from loom.config import MODEL_TIERS
from loom.contracts import Response
from loom.testing.fake_llm import FakeLLM
from tests.test_pipeline import DESIGN_OUT, PRD_OUT, VALIDATION, say, shell, write

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    assert runner.invoke(cli.app, ["init", "-C", str(p)]).exit_code == 0
    return p


@pytest.fixture()
def model(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """One `FakeLLM` behind every provider the CLI builds, scripted for validate→design."""
    fake = FakeLLM([say(VALIDATION), say(PRD_OUT), say(DESIGN_OUT)])
    monkeypatch.setattr(cli, "LiteLLMProvider", lambda *a, **k: fake)
    return fake


def run(*args: str) -> Any:
    return runner.invoke(cli.app, list(args), catch_exceptions=False)


# --------------------------------------------------------------------------- FR-HEADLESS-01


def test_a_run_with_yes_needs_no_terminal(project: Path, model: FakeLLM) -> None:
    result = run("run", "a url shortener", "-C", str(project), "--to", "design", "--yes")
    assert result.exit_code == cli.ExitCode.OK
    for name in ("validation.json", "prd.json", "design.json"):
        assert name in result.stdout
    assert model.call_count == 3


def test_the_artifacts_are_where_the_output_says_they_are(project: Path, model: FakeLLM) -> None:
    run("run", "a url shortener", "-C", str(project), "--to", "design", "--yes")
    runs = sorted((project / ".loom" / "artifacts").iterdir())
    assert len(runs) == 1
    assert json.loads((runs[0] / "design.json").read_text())["summary"].startswith("a slugify")


def test_stdin_being_closed_changes_nothing(project: Path, model: FakeLLM) -> None:
    """FR-HEADLESS-01, literally: a full pipeline runs with stdin closed."""
    result = runner.invoke(
        cli.app,
        ["run", "a url shortener", "-C", str(project), "--to", "design", "--yes"],
        input="",
    )
    assert result.exit_code == cli.ExitCode.OK


# --------------------------------------------------------------------------- FR-HEADLESS-02/03


def test_piped_output_is_plain_lines_with_no_escape_byte(project: Path, model: FakeLLM) -> None:
    """FR-HEADLESS-02 — CliRunner's stdout is not a TTY, so the per-turn line renderer is live.
    One readable line per turn, and not a single ESC byte in the whole stream."""
    result = run("run", "a url shortener", "-C", str(project), "--to", "design", "--yes")
    assert result.exit_code == cli.ExitCode.OK
    assert "\x1b" not in result.stdout
    turn_lines = [ln for ln in result.stdout.splitlines() if " turn " in ln and "$" in ln]
    assert len(turn_lines) == 3  # one per phase, each phase answering in a single turn


def test_json_flag_emits_only_parsable_json(project: Path, model: FakeLLM) -> None:
    """FR-HEADLESS-03 — every stdout line parses, and the human report is gone."""
    result = run("run", "a url shortener", "-C", str(project), "--to", "design", "--yes", "--json")
    assert result.exit_code == cli.ExitCode.OK
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert lines, "expected a JSON event stream on stdout"
    # Every line parsing is also proof the human `_report` block is gone — its lines
    # ("run <id>: passed (...)") are not JSON and would raise here if they had leaked.
    kinds = {json.loads(ln)["kind"] for ln in lines}
    assert {"run_started", "turn", "run_finished"} <= kinds


# --------------------------------------------------------------------------- FR-CLI-06


def test_a_budget_that_cannot_finish_exits_three(project: Path, model: FakeLLM) -> None:
    """FR-PIPE-03 — aborts into a resumable state, with the budget exit code."""
    result = run(
        "run",
        "a url shortener",
        "-C",
        str(project),
        "--to",
        "design",
        "--yes",
        "--budget",
        "0.01",
    )
    assert result.exit_code == cli.ExitCode.BUDGET
    assert model.call_count == 1
    assert (project / ".loom" / "runs").is_dir()


def test_an_uninitialised_project_is_a_usage_error(tmp_path: Path) -> None:
    result = run("run", "an idea", "-C", str(tmp_path / "nowhere"), "--yes")
    assert result.exit_code == cli.ExitCode.USAGE
    assert "loom init" in result.stdout


def test_a_phase_name_that_is_not_a_phase_is_a_usage_error(project: Path) -> None:
    result = run("run", "an idea", "-C", str(project), "--from", "deploy", "--yes")
    assert result.exit_code == cli.ExitCode.USAGE
    assert "unknown phase" in result.stdout


def test_every_status_maps_to_an_exit_code() -> None:
    """A status with no mapping would fall through to 1, which reads as a crash."""
    from loom.pipeline import Status

    assert set(Status.__args__) <= set(cli.EXIT_FOR_STATUS)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- FR-PIPE-04


def test_a_single_phase_runs_against_what_is_already_on_disk(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`loom validate` then `loom plan` — two commands, one run, no re-research."""
    first = FakeLLM([say(VALIDATION)])
    monkeypatch.setattr(cli, "LiteLLMProvider", lambda *a, **k: first)
    assert run("validate", "a url shortener", "-C", str(project), "--yes").exit_code == 0

    second = FakeLLM([say(PRD_OUT)])
    monkeypatch.setattr(cli, "LiteLLMProvider", lambda *a, **k: second)
    result = run("plan", "-C", str(project), "--yes")

    assert result.exit_code == cli.ExitCode.OK
    assert second.call_count == 1
    assert "a url shortener" in second.calls[0][1]["content"]
    runs = sorted((project / ".loom" / "artifacts").iterdir())
    assert len(runs) == 1  # the same run, not a second one
    assert {p.name for p in runs[0].iterdir()} == {"validation.json", "prd.json"}


def test_a_phase_with_nothing_upstream_says_so(project: Path, model: FakeLLM) -> None:
    result = run("design", "-C", str(project), "--yes")
    assert result.exit_code == cli.ExitCode.USAGE
    assert "no run to continue" in result.stdout


# --------------------------------------------------------------------------- FR-SESS-04


@pytest.mark.slow
def test_resume_picks_up_at_the_first_missing_artifact(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two commands, one run: `loom run --to plan`, then `loom resume` finishes it. Slow
    because resume runs to the end, and the end is a real build in a real repository."""
    first = FakeLLM([say(VALIDATION), say(PRD_OUT)])
    monkeypatch.setattr(cli, "LiteLLMProvider", lambda *a, **k: first)
    run("run", "a url shortener", "-C", str(project), "--to", "plan", "--yes")

    buildable = {
        **DESIGN_OUT,
        "rubric": {
            "threshold": 0.85,
            "hard_fail": [],
            "criteria": [
                shell("exists", 0.5, "test -f slugify.py"),
                shell("tests", 0.5, "python3 -m pytest -q"),
            ],
        },
    }
    second = FakeLLM(
        [
            say(buildable),
            write("slugify.py", "x = 1\n"),
            write("test_slugify.py", "def test_ok():\n    assert True\n", "c2"),
            Response(text="built it"),
        ]
    )
    monkeypatch.setattr(cli, "LiteLLMProvider", lambda *a, **k: second)
    result = run("resume", "-C", str(project), "--yes")

    assert "resuming" in result.stdout and "design" in result.stdout
    assert result.exit_code == cli.ExitCode.OK
    assert (project / "slugify.py").is_file()
    artifacts = sorted((project / ".loom" / "artifacts").iterdir())[0]
    assert {p.name for p in artifacts.iterdir()} == {
        "validation.json",
        "prd.json",
        "design.json",
        "score.json",
    }


def test_resume_with_nothing_to_resume_says_so(project: Path) -> None:
    result = run("resume", "-C", str(project))
    assert result.exit_code == cli.ExitCode.USAGE
    assert "no run to resume" in result.stdout


# --------------------------------------------------------------------------- FR-SESS-05


def test_replay_prints_a_diff_and_writes_a_new_slot(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = FakeLLM([say(VALIDATION), say(PRD_OUT), say(DESIGN_OUT)])
    monkeypatch.setattr(cli, "LiteLLMProvider", lambda *a, **k: first)
    run("run", "a url shortener", "-C", str(project), "--to", "design", "--yes")
    run_id = sorted((project / ".loom" / "artifacts").iterdir())[0].name

    again = FakeLLM([say({**DESIGN_OUT, "summary": "rewritten"})])
    monkeypatch.setattr(cli, "LiteLLMProvider", lambda *a, **k: again)
    result = run("replay", run_id, "--phase", "design", "-C", str(project))

    assert result.exit_code == cli.ExitCode.OK
    assert again.call_count == 1
    assert "rewritten" in result.stdout
    assert "design.replay-1.json" in result.stdout
    assert (project / ".loom" / "artifacts" / run_id / "design.replay-1.json").is_file()


def test_replaying_the_build_is_refused(project: Path, model: FakeLLM) -> None:
    result = run("replay", "nosuchrun", "--phase", "build", "-C", str(project))
    assert result.exit_code == cli.ExitCode.USAGE


# --------------------------------------------------------------------------- FR-COST-05


def test_cost_reports_by_phase_and_by_model(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ledger is written by the real provider, which is exactly what is stubbed out here —
    so this asserts the report, and `test_ledger.py` asserts the arithmetic."""
    from loom.ledger import Ledger

    ledger = Ledger(cli.ledger_path(project))
    ledger.record(phase="design", model="qwen", in_tok=10, out_tok=5, usd=0.02, seconds=1.0)
    ledger.record(phase="judge", model="qwen", in_tok=10, out_tok=5, usd=0.01, seconds=1.0)

    result = run("cost", "-C", str(project))
    assert result.exit_code == cli.ExitCode.OK
    assert "$0.0300" in result.stdout
    assert "design" in result.stdout and "judge" in result.stdout
    assert "qwen" in result.stdout


# --------------------------------------------------------------------------- flags


def test_the_model_flag_beats_the_config_file(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-CFG-01 at the command line, on the value that actually costs money."""
    (project / ".loom" / "config.toml").write_text('model = "from/the-file"\n', encoding="utf-8")
    seen: list[str] = []

    def spy(model: str, *a: Any, **k: Any) -> FakeLLM:
        seen.append(model)
        return FakeLLM([say(VALIDATION)])

    monkeypatch.setattr(cli, "LiteLLMProvider", spy)
    run("validate", "an idea", "-C", str(project), "--yes", "--model", "from/the-flag")
    assert "from/the-flag" in seen
    assert "from/the-file" not in seen


def test_no_cache_makes_the_second_run_pay_again(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def use(fake: FakeLLM) -> FakeLLM:
        monkeypatch.setattr(cli, "LiteLLMProvider", lambda *a, **k: fake)
        return fake

    for flags, expected in ((["--yes"], 0), (["--yes", "--no-cache"], 1)):
        use(FakeLLM([say(VALIDATION)]))
        run("validate", "a url shortener", "-C", str(project), "--yes")

        second = use(FakeLLM([say(VALIDATION)]))
        run("validate", "a url shortener", "-C", str(project), *flags)
        assert second.call_count == expected


def test_an_unattended_run_without_a_budget_is_refused(project: Path, model: FakeLLM) -> None:
    """FR-HEADLESS-04."""
    (project / ".loom" / "config.toml").write_text('mode = "unattended"\n', encoding="utf-8")
    result = run("run", "an idea", "-C", str(project), "--to", "design")
    assert result.exit_code == cli.ExitCode.USAGE
    assert "--budget" in result.stdout


def test_the_judge_is_a_separate_provider_from_the_builder(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cost rules pin the judge cheap independently of `--model`; if they were the same
    provider, `--model anthropic/...` would quietly make every judge call expensive."""
    built: list[str] = []
    monkeypatch.setattr(
        cli,
        "LiteLLMProvider",
        lambda model, *a, **k: (built.append(model), FakeLLM([say(VALIDATION)]))[1],
    )
    run("validate", "an idea", "-C", str(project), "--yes", "--model", "anthropic/claude-sonnet-5")
    assert "anthropic/claude-sonnet-5" in built
    assert MODEL_TIERS["cheap"] in built  # the judge stayed on the pinned cheap tier


def test_a_provider_that_says_nothing_is_reported_not_swallowed(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three unusable outputs is a `PhaseError`, which is exit 1 and a readable message."""
    monkeypatch.setattr(cli, "LiteLLMProvider", lambda *a, **k: FakeLLM([Response(text="hi")] * 3))
    result = run("validate", "an idea", "-C", str(project), "--yes")
    assert result.exit_code == cli.ExitCode.ERROR
    assert "attempt 1" in result.stdout


def test_a_gated_run_with_no_terminal_says_so_rather_than_crashing(
    project: Path, model: FakeLLM
) -> None:
    """FR-HEADLESS-01's other half. Auto-approving would be friendlier and wrong: nobody piping
    into a command expects it to spend money unsupervised because it could not find them."""
    result = run("run", "an idea", "-C", str(project), "--to", "design")
    assert result.exit_code == cli.ExitCode.USAGE
    assert "--yes" in result.stdout
    assert model.call_count == 0
