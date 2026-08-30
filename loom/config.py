"""Configuration and its precedence: flag > env > .loom/config.toml > ~/.loom/config.toml.

FR-CFG-01/02. The effort preset table (FR-SEL-03) lives here as data, not in the widget that
renders it, so a user override in config.toml beats the preset.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONFIG_NAME = "config.toml"
CREDENTIALS_NAME = "credentials.json"
ENV_PREFIX = "LOOM_"

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Mode = Literal["auto", "strict", "plan-only", "unattended"]
Tier = Literal["cheap", "strong"]


class EffortPreset(BaseModel):
    """One row of the FR-SEL-03 table."""

    tier: Tier
    max_turns: int = Field(gt=0)
    max_usd: float = Field(gt=0)
    rubric_rounds: int = Field(gt=0)


#: FR-SEL-03, verbatim. Changing a number here changes what every effort level means.
EFFORT_PRESETS: dict[str, EffortPreset] = {
    "low": EffortPreset(tier="cheap", max_turns=12, max_usd=0.25, rubric_rounds=1),
    "medium": EffortPreset(tier="cheap", max_turns=25, max_usd=1.00, rubric_rounds=2),
    "high": EffortPreset(tier="cheap", max_turns=40, max_usd=2.00, rubric_rounds=3),
    "xhigh": EffortPreset(tier="strong", max_turns=60, max_usd=4.00, rubric_rounds=4),
    "max": EffortPreset(tier="strong", max_turns=100, max_usd=8.00, rubric_rounds=6),
}

DEFAULT_EFFORT: Effort = "medium"

#: Which model an effort tier resolves to when the user has not named one.
MODEL_TIERS: dict[str, str] = {
    "cheap": "openrouter/qwen/qwen3-coder",
    "strong": "anthropic/claude-sonnet-5",
}


class Price(BaseModel):
    """USD per million tokens. FR-COST-02's fallback when litellm cannot price a model."""

    model_config = ConfigDict(extra="forbid")

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


#: Indicative prices for the models Loom names itself, from SRS §3.4 as of 2026-08-30. Used only
#: when `litellm.completion_cost()` cannot price a model string; a stale price beats a crash
#: after the tokens are already paid for.
DEFAULT_PRICE_TABLE: dict[str, Price] = {
    "openrouter/qwen/qwen3-coder": Price(input_per_mtok=0.30, output_per_mtok=0.30),
    "openrouter/qwen/qwen3-coder-480b": Price(input_per_mtok=0.90, output_per_mtok=0.90),
    "anthropic/claude-sonnet-5": Price(input_per_mtok=3.00, output_per_mtok=15.00),
}


class Config(BaseModel):
    """Extra keys are refused: a typo in a config file should be loud, not ignored (FR-CFG-02).

    That refusal is also why an API key cannot be parked here — `api_key` is not a field, so
    `.loom/config.toml` rejects it (FR-CFG-06 keeps keys in the environment or in
    `~/.loom/credentials.json`).
    """

    model_config = ConfigDict(extra="forbid")

    effort: Effort = DEFAULT_EFFORT
    mode: Mode = "auto"

    # Defaults below match DEFAULT_EFFORT's preset; the validator re-derives them whenever the
    # effort differs and the key was not set explicitly at any layer.
    model: str = MODEL_TIERS["cheap"]
    max_turns: int = Field(default=25, gt=0)
    max_usd: float = Field(default=1.00, gt=0)
    rubric_rounds: int = Field(default=2, gt=0)

    # Pinned cheap by the cost rules, independently of effort — the judge is graded on being
    # consistent and free, not on being clever.
    judge_model: str = MODEL_TIERS["cheap"]
    budget_usd: float = Field(default=5.0, gt=0)

    theme: str | None = None
    blueprint: str | None = None
    price_table: dict[str, Price] = Field(default_factory=lambda: dict(DEFAULT_PRICE_TABLE))

    @model_validator(mode="before")
    @classmethod
    def _apply_effort_preset(cls, data: Any) -> Any:
        """FR-SEL-03: effort supplies the knobs the user did not set. Explicit always wins."""
        if not isinstance(data, dict):
            return data
        preset = EFFORT_PRESETS.get(str(data.get("effort", DEFAULT_EFFORT)))
        if preset is None:
            return data  # unknown effort — let the Literal produce the error
        filled = dict(data)
        filled.setdefault("max_turns", preset.max_turns)
        filled.setdefault("max_usd", preset.max_usd)
        filled.setdefault("rubric_rounds", preset.rubric_rounds)
        filled.setdefault("model", MODEL_TIERS[preset.tier])
        return filled

    @property
    def preset(self) -> EffortPreset:
        return EFFORT_PRESETS[self.effort]


def project_config_path(cwd: Path) -> Path:
    return Path(cwd) / ".loom" / CONFIG_NAME


def user_config_path(home: Path) -> Path:
    return Path(home) / ".loom" / CONFIG_NAME


def credentials_path(home: Path) -> Path:
    """FR-CFG-06 — provider keys live here, never in a project config."""
    return Path(home) / ".loom" / CREDENTIALS_NAME


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _from_env(env: Mapping[str, str]) -> dict[str, Any]:
    fields = set(Config.model_fields)
    out: dict[str, Any] = {}
    for key, value in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        name = key[len(ENV_PREFIX) :].lower()
        # An empty env var is a shell artifact, not an instruction to blank the setting.
        if name in fields and value != "":
            out[name] = value  # pydantic does the coercion
    return out


def load_config(
    *,
    flags: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
) -> Config:
    """Lowest precedence first; each layer only contributes the keys it actually sets.

    Paths and env are arguments rather than globals so all four levels are testable without
    monkeypatching `Path.home`.
    """
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    home = Path(home) if home is not None else Path.home()
    env = os.environ if env is None else env

    merged: dict[str, Any] = {}
    merged.update(_read_toml(user_config_path(home)))
    merged.update(_read_toml(project_config_path(cwd)))
    merged.update(_from_env(env))
    merged.update({k: v for k, v in (flags or {}).items() if v is not None})
    return Config(**merged)


def config_sources(
    *,
    flags: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    """Which layer each key came from. `/config` (FR-CFG-04) renders this beside each value."""
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    home = Path(home) if home is not None else Path.home()
    env = os.environ if env is None else env

    layers = [
        ("user config", _read_toml(user_config_path(home))),
        ("project config", _read_toml(project_config_path(cwd))),
        ("env", _from_env(env)),
        ("flag", {k: v for k, v in (flags or {}).items() if v is not None}),
    ]
    sources = {name: "default" for name in Config.model_fields}
    for label, layer in layers:
        for key in layer:
            if key in sources:
                sources[key] = label
    effort_derived = {"max_turns", "max_usd", "rubric_rounds", "model"}
    for key in effort_derived:
        if sources[key] == "default" and sources["effort"] != "default":
            sources[key] = f"effort preset ({sources['effort']})"
    return sources
