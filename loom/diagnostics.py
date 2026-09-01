"""`loom doctor` and `loom bug` — FR-DIAG-02/03.

Core, not terminal: every function returns data — `Check` rows, a bundle `Path` — and the CLI and
the REPL render it. Two rules the SRS makes non-negotiable, kept here by construction:

* **doctor prints no secret.** A credential check reports *presence*, never the value; the value
  never enters a `Check`.
* **the bug bundle carries no key.** Everything assembled is passed through `security.redact` and
  an exact strip of every `*_KEY` / `*_TOKEN` value, so a planted key appears nowhere — and nothing
  is uploaded: the bundle is a local file and its path is all that is returned (R1).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from loom.agent.providers import key_variable_for
from loom.config import Config, load_config
from loom.security import SECRET_NAME, redact
from loom.session import LOOM_DIR, Session

#: The lowest Python Loom supports (pyproject `requires-python`). Below it, litellm and the tomllib
#: import surface both misbehave, so it is a hard check rather than a warning.
MIN_PYTHON = (3, 11)

#: Free space under which a build — a `uv` venv plus a git history — starts failing in confusing
#: ways. Cheap to check, and the failure it prevents reads as a Loom bug rather than a full disk.
MIN_FREE_MB = 500

#: The host each provider's API lives at, for the reachability probe. Partial on purpose: an
#: unknown provider is reported as skipped rather than pinged at a guessed address.
PROVIDER_HOSTS: dict[str, str] = {
    "anthropic": "api.anthropic.com",
    "openrouter": "openrouter.ai",
    "nvidia_nim": "integrate.api.nvidia.com",
    "openai": "api.openai.com",
    "dashscope": "dashscope-intl.aliyuncs.com",
    "gemini": "generativelanguage.googleapis.com",
}


@dataclass(frozen=True)
class Check:
    """One diagnostic. `remedy` is shown only when `ok` is false — the fix, in one line."""

    name: str
    ok: bool
    detail: str
    remedy: str = ""


def format_report(checks: list[Check], *, ok: str = "ok", fail: str = "FAIL") -> str:
    """The checks as text, one per line, remedy indented under each failure. Glyphs are passed in
    (the theme's ✓/✗ from the REPL, plain words from a pipe) so this stays glyph-agnostic."""
    lines = []
    for c in checks:
        lines.append(f"  {ok if c.ok else fail:<4} {c.name:<11} {c.detail}")
        if not c.ok and c.remedy:
            lines.append(f"       → {c.remedy}")
    return "\n".join(lines)


def _reach(host: str, *, port: int = 443, timeout: float = 3.0) -> bool:
    """A plain TCP connect — reachability, not a real API call, so it needs no key and spends
    nothing. Injected in tests, where the socket block would refuse it anyway."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def doctor(
    root: Path | str,
    *,
    config: Config | None = None,
    env: Mapping[str, str] | None = None,
    reach: Callable[[str], bool] = _reach,
) -> list[Check]:
    """The install's health, one `Check` per line. Never raises and never prints a secret — a
    broken config still yields a row rather than a traceback."""
    root = Path(root)
    env = os.environ if env is None else env
    try:
        config = config or load_config(cwd=root)
    except Exception as exc:  # noqa: BLE001 - a bad config is a check result, not a crash
        return [Check("config", False, f"config is unreadable: {exc}", "fix .loom/config.toml")]

    return [
        _check_python(),
        _check_git(),
        _check_credential(config, env),
        _check_reach(config, env, reach),
        _check_loom_writable(root),
        _check_disk(root),
    ]


def _check_python() -> Check:
    ok = sys.version_info[:2] >= MIN_PYTHON
    want = ".".join(str(p) for p in MIN_PYTHON)
    remedy = "" if ok else f"upgrade to Python {want}+"
    return Check("python", ok, platform.python_version(), remedy)


def _check_git() -> Check:
    path = shutil.which("git")
    return Check("git", path is not None, path or "not found", "install git and add it to PATH")


def _check_credential(config: Config, env: Mapping[str, str]) -> Check:
    """Presence only — the value is never read into the result (FR-DIAG-02)."""
    var = key_variable_for(config.model)
    if not var:
        return Check("credential", True, f"{config.model} needs no API key (local model)")
    present = bool(env.get(var))
    return Check(
        "credential",
        present,
        f"{var} is set" if present else f"{var} is not set",
        f"export {var}=… or add it to ~/.loom/credentials.json",
    )


def _check_reach(config: Config, env: Mapping[str, str], reach: Callable[[str], bool]) -> Check:
    host = PROVIDER_HOSTS.get(config.model.split("/")[0])
    if host is None:
        return Check("provider", True, f"reachability not checked for {config.model}")
    ok = reach(host)
    state = "reachable" if ok else "unreachable"
    return Check("provider", ok, f"{host} {state}", "check your network")


def _check_loom_writable(root: Path) -> Check:
    d = root / LOOM_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return Check("workspace", True, f"{d} is writable")
    except OSError as exc:
        return Check("workspace", False, f"{d}: {exc}", "check directory permissions")


def _check_disk(root: Path) -> Check:
    try:
        free_mb = shutil.disk_usage(root).free // (1024 * 1024)
    except OSError as exc:  # pragma: no cover - stat failing on a live dir is rare
        return Check("disk", False, str(exc), "free up disk space")
    ok = free_mb >= MIN_FREE_MB
    return Check("disk", ok, f"{free_mb} MB free", f"free at least {MIN_FREE_MB} MB")


# --------------------------------------------------------------------------- FR-DIAG-03


def _versions() -> dict[str, str]:
    try:
        loom = _pkg_version("loom-cli")
    except PackageNotFoundError:  # pragma: no cover - only from an unbuilt tree
        loom = "0+unknown"
    return {"loom": loom, "python": platform.python_version(), "platform": platform.platform()}


def _key_token_values(env: Mapping[str, str]) -> set[str]:
    """Every `*_KEY` / `*_TOKEN` value, at any length. `security.redact` skips secrets under 8
    chars; the bundle strips these regardless, so a short planted key still cannot survive."""
    out = set()
    for name, value in env.items():
        if value and (name.upper().endswith("_KEY") or name.upper().endswith("_TOKEN")):
            out.add(value)
    return out


def bug_bundle(
    root: Path | str,
    *,
    dest: Path | None = None,
    env: Mapping[str, str] | None = None,
    config: Config | None = None,
    now: datetime | None = None,
) -> Path:
    """Write a redacted diagnostic bundle and return its path. Nothing is uploaded (R1) — the
    path is the whole output. FR-DIAG-03: config with secrets stripped, the last 200 events, the
    last score, versions.
    """
    root = Path(root)
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)

    try:
        cfg = config or load_config(cwd=root)
        config_dump: Any = _strip_secret_keys(cfg.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001 - a broken config still belongs in the report
        config_dump = {"error": str(exc)}

    session = Session.latest(root)
    events = session.read_events()[-200:] if session else []
    bundle = {
        "generated": now.isoformat(),
        "versions": _versions(),
        "config": config_dump,
        "run_id": session.run_id if session else None,
        "last_score": _last_score(root, session),
        "events": events,
    }

    text = json.dumps(bundle, indent=2, default=str)
    # Two passes: security.redact for anything key-shaped in env, then an exact strip of every
    # *_KEY/*_TOKEN value at any length. Neither trusts the other; a key survives neither.
    text = redact(text, env=env, extra=_key_token_values(env))
    for value in sorted(_key_token_values(env), key=len, reverse=True):
        text = text.replace(value, "[redacted]")

    dest = dest or root / LOOM_DIR / f"bug-{now.strftime('%Y%m%dT%H%M%S')}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text + "\n", encoding="utf-8")
    return dest


def _strip_secret_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Drop any config key whose *name* looks secret. Config forbids `api_key` already (FR-CFG-06),
    so this is defence in depth against a future field, not a live leak."""
    return {k: v for k, v in data.items() if not SECRET_NAME.search(k)}


def _last_score(root: Path, session: Session | None) -> Any:
    """The build's `score.json` for the latest run, if it exists — the last grading, inspectable
    after the fact (FR-ART-04). Missing or torn is `None`, never an error."""
    if session is None:
        return None
    path = root / LOOM_DIR / "artifacts" / session.run_id / "score.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
