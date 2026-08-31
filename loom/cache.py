"""Content-addressed phase cache. FR-PIPE-05, WP-3.0.

The expensive thing in this project is a provider call, and the most-repeated action is
re-running a phase whose inputs did not change — while tuning the *next* phase's prompt, or
while a reject-and-retry loop revisits an upstream artifact. Keying on everything that could
change the answer means a hit is safe to serve and a miss is honest.

Everything in the key is a value, not a timestamp or a path, so the cache is portable and
deleting `.loom/cache/` costs nothing but the next run's money.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loom.agent.tools.fs import atomic_write

#: Bumped when the meaning of a stored entry changes. A stale entry from an older Loom is then
#: a miss rather than a wrong answer.
VERSION = "1"

#: The config keys that can change a phase's output. Deliberately narrow: `theme` cannot, and
#: including it would bust every entry the first time somebody tried a colour.
CONFIG_SLICE = ("model", "max_turns", "max_usd", "effort", "blueprint")


def config_slice(config: Any) -> dict[str, Any]:
    """The part of a `Config` that belongs in a cache key."""
    return {k: getattr(config, k, None) for k in CONFIG_SLICE}


def cache_key(
    *,
    phase: str,
    prompt: str,
    task: str,
    model: str,
    config: Mapping[str, Any] | None = None,
) -> str:
    """sha256 over every input that could change the artifact.

    Parts are NUL-separated so that ("ab", "c") and ("a", "bc") cannot collide — the classic
    way a concatenated hash key quietly serves one phase another phase's answer.
    """
    digest = hashlib.sha256()
    parts = [
        VERSION,
        phase,
        prompt,
        task,
        model,
        json.dumps(dict(config or {}), sort_keys=True, default=str),
    ]
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


class PhaseCache:
    """`.loom/cache/<key>.json`, one file per entry, always safe to delete.

    Disabled (`--no-cache`) is not a second code path: `get` misses and `put` is a no-op, so
    every caller stays identical.
    """

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled

    @property
    def dir(self) -> Path:
        return self.root / ".loom" / "cache"

    def path_for(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> str | None:
        """The stored artifact JSON, or `None`. A corrupt entry is a miss, never an error."""
        if not self.enabled:
            return None
        path = self.path_for(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        value = payload.get("artifact") if isinstance(payload, dict) else None
        return value if isinstance(value, str) else None

    def put(self, key: str, artifact_json: str, *, phase: str = "") -> Path | None:
        """Store, atomically. Returns the path, or `None` when the cache is off."""
        if not self.enabled:
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(key)
        # `phase` is carried for the human who runs `ls .loom/cache` wondering what a hex name is.
        payload = json.dumps({"phase": phase, "artifact": artifact_json}, indent=2) + "\n"
        atomic_write(path, payload)
        return path
