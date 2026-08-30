from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from loom.config import Config, load_config, project_config_path, user_config_path


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "home", tmp_path / "project"


def test_defaults_when_nothing_is_configured(dirs: tuple[Path, Path]) -> None:
    home, cwd = dirs
    assert load_config(env={}, cwd=cwd, home=home) == Config()


def test_user_config_is_the_lowest_level(dirs: tuple[Path, Path]) -> None:
    home, cwd = dirs
    _write(user_config_path(home), 'model = "from-user"\nmax_turns = 11\n')
    cfg = load_config(env={}, cwd=cwd, home=home)
    assert cfg.model == "from-user" and cfg.max_turns == 11


def test_project_config_beats_user_config(dirs: tuple[Path, Path]) -> None:
    home, cwd = dirs
    _write(user_config_path(home), 'model = "from-user"\nmax_turns = 11\n')
    _write(project_config_path(cwd), 'model = "from-project"\n')
    cfg = load_config(env={}, cwd=cwd, home=home)
    assert cfg.model == "from-project"
    assert cfg.max_turns == 11  # untouched keys fall through, they are not reset


def test_env_beats_project_config(dirs: tuple[Path, Path]) -> None:
    home, cwd = dirs
    _write(project_config_path(cwd), 'model = "from-project"\nmax_usd = 1.5\n')
    cfg = load_config(env={"LOOM_MODEL": "from-env"}, cwd=cwd, home=home)
    assert cfg.model == "from-env" and cfg.max_usd == 1.5


def test_flag_beats_everything(dirs: tuple[Path, Path]) -> None:
    home, cwd = dirs
    _write(user_config_path(home), 'model = "from-user"\n')
    _write(project_config_path(cwd), 'model = "from-project"\n')
    cfg = load_config(
        flags={"model": "from-flag"}, env={"LOOM_MODEL": "from-env"}, cwd=cwd, home=home
    )
    assert cfg.model == "from-flag"


def test_all_four_levels_at_once(dirs: tuple[Path, Path]) -> None:
    home, cwd = dirs
    _write(user_config_path(home), 'model = "u"\njudge_model = "u"\nmax_turns = 1\nmax_usd = 1.0\n')
    _write(project_config_path(cwd), 'judge_model = "p"\nmax_turns = 2\nmax_usd = 2.0\n')
    cfg = load_config(
        flags={"max_usd": 4.0},
        env={"LOOM_MAX_TURNS": "3"},
        cwd=cwd,
        home=home,
    )
    assert (cfg.model, cfg.judge_model, cfg.max_turns, cfg.max_usd) == ("u", "p", 3, 4.0)


def test_env_values_are_coerced_by_type(dirs: tuple[Path, Path]) -> None:
    home, cwd = dirs
    cfg = load_config(env={"LOOM_MAX_TURNS": "7", "LOOM_MAX_USD": "0.25"}, cwd=cwd, home=home)
    assert cfg.max_turns == 7 and isinstance(cfg.max_turns, int)
    assert cfg.max_usd == 0.25


def test_unrelated_and_empty_env_vars_are_ignored(dirs: tuple[Path, Path]) -> None:
    home, cwd = dirs
    env = {"LOOM_MODEL": "", "MODEL": "nope", "LOOM_NOT_A_FIELD": "x", "PATH": "/usr/bin"}
    assert load_config(env=env, cwd=cwd, home=home) == Config()


def test_none_flags_do_not_override(dirs: tuple[Path, Path]) -> None:
    home, cwd = dirs
    _write(project_config_path(cwd), 'model = "from-project"\n')
    cfg = load_config(flags={"model": None, "max_turns": None}, env={}, cwd=cwd, home=home)
    assert cfg.model == "from-project" and cfg.max_turns == Config().max_turns


def test_a_typo_in_a_config_file_is_loud(dirs: tuple[Path, Path]) -> None:
    home, cwd = dirs
    _write(project_config_path(cwd), 'moddel = "typo"\n')
    with pytest.raises(ValidationError, match="moddel"):
        load_config(env={}, cwd=cwd, home=home)


def test_out_of_range_values_are_rejected(dirs: tuple[Path, Path]) -> None:
    home, cwd = dirs
    with pytest.raises(ValidationError):
        load_config(env={"LOOM_MAX_TURNS": "0"}, cwd=cwd, home=home)
    with pytest.raises(ValidationError):
        load_config(flags={"max_usd": -1.0}, env={}, cwd=cwd, home=home)
