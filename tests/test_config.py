"""FR-CFG-01, FR-CFG-02, FR-SEL-03, FR-COST-02, SRS §6 config keys."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from loom.config import (
    DEFAULT_EFFORT,
    DEFAULT_PRICE_TABLE,
    EFFORT_PRESETS,
    MODEL_TIERS,
    Config,
    Price,
    config_sources,
    credentials_path,
    load_config,
    project_config_path,
    user_config_path,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "home", tmp_path / "project"


# --------------------------------------------------------------------------- precedence


def test_defaults_when_nothing_is_configured(dirs: tuple[Path, Path]) -> None:
    """FR-CFG-01 — the built-in default is the bottom layer."""
    home, cwd = dirs
    assert load_config(env={}, cwd=cwd, home=home) == Config()


def test_user_config_is_the_lowest_file_layer(dirs: tuple[Path, Path]) -> None:
    """FR-CFG-01."""
    home, cwd = dirs
    _write(user_config_path(home), 'model = "from-user"\nmax_turns = 11\n')
    cfg = load_config(env={}, cwd=cwd, home=home)
    assert cfg.model == "from-user" and cfg.max_turns == 11


def test_project_config_beats_user_config(dirs: tuple[Path, Path]) -> None:
    """FR-CFG-01."""
    home, cwd = dirs
    _write(user_config_path(home), 'model = "from-user"\nmax_turns = 11\n')
    _write(project_config_path(cwd), 'model = "from-project"\n')
    cfg = load_config(env={}, cwd=cwd, home=home)
    assert cfg.model == "from-project"
    assert cfg.max_turns == 11  # untouched keys fall through, they are not reset


def test_env_beats_project_config(dirs: tuple[Path, Path]) -> None:
    """FR-CFG-01."""
    home, cwd = dirs
    _write(project_config_path(cwd), 'model = "from-project"\nmax_usd = 1.5\n')
    cfg = load_config(env={"LOOM_MODEL": "from-env"}, cwd=cwd, home=home)
    assert cfg.model == "from-env" and cfg.max_usd == 1.5


def test_flag_beats_everything(dirs: tuple[Path, Path]) -> None:
    """FR-CFG-01."""
    home, cwd = dirs
    _write(user_config_path(home), 'model = "from-user"\n')
    _write(project_config_path(cwd), 'model = "from-project"\n')
    cfg = load_config(
        flags={"model": "from-flag"}, env={"LOOM_MODEL": "from-env"}, cwd=cwd, home=home
    )
    assert cfg.model == "from-flag"


def test_all_four_levels_at_once(dirs: tuple[Path, Path]) -> None:
    """FR-CFG-01 — one key resolved from each layer in the same load."""
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


# --------------------------------------------------------------------------- refusal


def test_a_typo_in_a_config_file_is_loud(dirs: tuple[Path, Path]) -> None:
    """FR-CFG-02."""
    home, cwd = dirs
    _write(project_config_path(cwd), 'moddel = "typo"\n')
    with pytest.raises(ValidationError, match="moddel"):
        load_config(env={}, cwd=cwd, home=home)


def test_a_provider_key_cannot_be_parked_in_a_project_config(dirs: tuple[Path, Path]) -> None:
    """FR-CFG-06 — keys live in the environment or in ~/.loom/credentials.json, never here."""
    home, cwd = dirs
    _write(project_config_path(cwd), 'api_key = "sk-not-here-please"\n')
    with pytest.raises(ValidationError, match="api_key"):
        load_config(env={}, cwd=cwd, home=home)
    assert credentials_path(home).name == "credentials.json"


def test_out_of_range_values_are_rejected(dirs: tuple[Path, Path]) -> None:
    home, cwd = dirs
    with pytest.raises(ValidationError):
        load_config(env={"LOOM_MAX_TURNS": "0"}, cwd=cwd, home=home)
    with pytest.raises(ValidationError):
        load_config(flags={"max_usd": -1.0}, env={}, cwd=cwd, home=home)


@pytest.mark.parametrize("key,bad", [("effort", "extreme"), ("mode", "yolo")])
def test_enumerated_keys_reject_anything_off_the_list(
    dirs: tuple[Path, Path], key: str, bad: str
) -> None:
    """SRS §3.5 and FR-SEL-03 fix these vocabularies."""
    home, cwd = dirs
    with pytest.raises(ValidationError, match=key):
        load_config(flags={key: bad}, env={}, cwd=cwd, home=home)


# --------------------------------------------------------------------------- effort presets


def test_the_preset_table_is_the_srs_table() -> None:
    """FR-SEL-03 — the mapping is data in config.py, and this is the data."""
    table = {
        name: (p.tier, p.max_turns, p.max_usd, p.rubric_rounds)
        for name, p in EFFORT_PRESETS.items()
    }
    assert table == {
        "low": ("cheap", 12, 0.25, 1),
        "medium": ("cheap", 25, 1.00, 2),
        "high": ("cheap", 40, 2.00, 3),
        "xhigh": ("strong", 60, 4.00, 4),
        "max": ("strong", 100, 8.00, 6),
    }


@pytest.mark.parametrize("effort", sorted(EFFORT_PRESETS))
def test_effort_supplies_every_knob_the_user_did_not_set(
    dirs: tuple[Path, Path], effort: str
) -> None:
    """FR-SEL-03."""
    home, cwd = dirs
    preset = EFFORT_PRESETS[effort]
    cfg = load_config(flags={"effort": effort}, env={}, cwd=cwd, home=home)
    assert cfg.max_turns == preset.max_turns
    assert cfg.max_usd == preset.max_usd
    assert cfg.rubric_rounds == preset.rubric_rounds
    assert cfg.model == MODEL_TIERS[preset.tier]
    assert cfg.preset == preset


def test_an_explicit_override_beats_the_preset(dirs: tuple[Path, Path]) -> None:
    """FR-SEL-03 — 'a user override in config.toml beats the preset', at any layer."""
    home, cwd = dirs
    _write(project_config_path(cwd), 'effort = "max"\nmax_turns = 7\n')
    cfg = load_config(env={}, cwd=cwd, home=home)
    assert cfg.effort == "max"
    assert cfg.max_turns == 7  # explicit
    assert cfg.max_usd == EFFORT_PRESETS["max"].max_usd  # preset
    assert cfg.model == MODEL_TIERS["strong"]  # preset

    from_env = load_config(env={"LOOM_EFFORT": "low", "LOOM_MODEL": "mine"}, cwd=cwd, home=home)
    assert from_env.model == "mine" and from_env.max_turns == 7


def test_the_default_effort_is_what_the_bare_defaults_encode() -> None:
    """FR-SEL-03 — the declared field defaults must not drift from the preset table."""
    preset = EFFORT_PRESETS[DEFAULT_EFFORT]
    cfg = Config()
    assert cfg.effort == DEFAULT_EFFORT
    assert (cfg.max_turns, cfg.max_usd, cfg.rubric_rounds) == (
        preset.max_turns,
        preset.max_usd,
        preset.rubric_rounds,
    )


def test_a_full_phase_sweep_fits_inside_the_run_budget() -> None:
    """NFR-COST-01 — four phases at the per-phase ceiling must not exceed the run ceiling."""
    cfg = Config()
    assert 4 * cfg.max_usd <= cfg.budget_usd


def test_the_judge_model_is_pinned_cheap_regardless_of_effort() -> None:
    """Cost rules — the judge is graded on being consistent and free, not clever."""
    assert Config(effort="max").judge_model == MODEL_TIERS["cheap"]
    assert Config(effort="max").model == MODEL_TIERS["strong"]


# --------------------------------------------------------------------------- new SRS keys


def test_the_srs_config_key_set_is_exactly_the_model_fields() -> None:
    """SRS §6 — the documented key list and the code must not drift."""
    assert set(Config.model_fields) == {
        "model",
        "judge_model",
        "max_turns",
        "max_usd",
        "budget_usd",
        "effort",
        "mode",
        "theme",
        "blueprint",
        "price_table",
        "rubric_rounds",
    }


def test_the_price_table_ships_prices_and_accepts_overrides(dirs: tuple[Path, Path]) -> None:
    """FR-COST-02 — an unpriced model must never raise mid-run, so the fallback is populated."""
    home, cwd = dirs
    default = Config()
    # Every tier Loom can pick for you is priced. The *number* is not the invariant — a free
    # model's is legitimately 0.0 — the presence of an entry is, because that is what stops
    # `_fallback_cost` guessing after the tokens are already paid for.
    for model in MODEL_TIERS.values():
        assert model in default.price_table
    assert default.price_table[MODEL_TIERS["strong"]].output_per_mtok == 15.00

    _write(
        project_config_path(cwd),
        '[price_table."local/whatever"]\ninput_per_mtok = 0.0\noutput_per_mtok = 0.0\n',
    )
    cfg = load_config(env={}, cwd=cwd, home=home)
    assert cfg.price_table["local/whatever"] == Price(input_per_mtok=0.0, output_per_mtok=0.0)


def test_a_malformed_price_entry_is_refused(dirs: tuple[Path, Path]) -> None:
    """FR-COST-02 — a wrong price is worse than a loud one."""
    home, cwd = dirs
    _write(project_config_path(cwd), '[price_table."m"]\ninput_per_mtok = -1.0\n')
    with pytest.raises(ValidationError):
        load_config(env={}, cwd=cwd, home=home)


def test_the_default_price_table_is_a_copy_not_shared_state() -> None:
    a, b = Config(), Config()
    a.price_table["scratch"] = Price(input_per_mtok=1.0, output_per_mtok=1.0)
    assert "scratch" not in b.price_table


# --------------------------------------------------------------------------- source reporting


def test_config_sources_names_the_layer_each_key_came_from(dirs: tuple[Path, Path]) -> None:
    """FR-CFG-04 — /config renders the source beside every value."""
    home, cwd = dirs
    _write(user_config_path(home), 'judge_model = "u"\n')
    _write(project_config_path(cwd), 'effort = "xhigh"\n')
    sources = config_sources(
        flags={"budget_usd": 9.0}, env={"LOOM_THEME": "dark"}, cwd=cwd, home=home
    )
    assert sources["judge_model"] == "user config"
    assert sources["effort"] == "project config"
    assert sources["theme"] == "env"
    assert sources["budget_usd"] == "flag"
    assert sources["max_turns"] == "effort preset (project config)"
    assert sources["mode"] == "default"


def test_config_sources_covers_every_key(dirs: tuple[Path, Path]) -> None:
    home, cwd = dirs
    assert set(config_sources(env={}, cwd=cwd, home=home)) == set(Config.model_fields)


def test_the_default_models_are_priced_and_have_a_known_key_variable() -> None:
    """Two ways a model swap silently degrades the tool: a price nobody entered makes every
    USD ceiling read $0.00, and a prefix nobody mapped makes a missing key an unhelpful error."""
    from loom.agent.providers import key_variable_for

    for tier, model in MODEL_TIERS.items():
        assert model in DEFAULT_PRICE_TABLE, f"the {tier} tier model has no price entry"
        assert key_variable_for(model), f"the {tier} tier model has no known key variable"
