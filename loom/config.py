"""Configuration and its precedence: flag > env > .loom/config.toml > ~/.loom/config.toml."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CONFIG_NAME = "config.toml"
ENV_PREFIX = "LOOM_"


class Config(BaseModel):
    """Extra keys are refused: a typo in a config file should be loud, not ignored."""

    model_config = ConfigDict(extra="forbid")

    # ponytail: placeholders until WP-0's four numbers land in docs/DECISIONS.md.
    model: str = "openrouter/qwen/qwen3-coder"
    judge_model: str = "openrouter/qwen/qwen3-coder"
    max_turns: int = Field(default=40, gt=0)
    max_usd: float = Field(default=2.0, gt=0)
    budget_usd: float = Field(default=5.0, gt=0)


def project_config_path(cwd: Path) -> Path:
    return Path(cwd) / ".loom" / CONFIG_NAME


def user_config_path(home: Path) -> Path:
    return Path(home) / ".loom" / CONFIG_NAME


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
