"""Every model call's cost, on disk, queryable. You cannot tune what you cannot see."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT    NOT NULL,
    run_id  TEXT    NOT NULL DEFAULT '',
    phase   TEXT    NOT NULL,
    model   TEXT    NOT NULL,
    in_tok  INTEGER NOT NULL DEFAULT 0,
    out_tok INTEGER NOT NULL DEFAULT 0,
    usd     REAL    NOT NULL DEFAULT 0.0,
    seconds REAL    NOT NULL DEFAULT 0.0
);
"""


class Ledger:
    """One sqlite file per project. `run_id` is not in the plan's schema; WP-5.1's per-run
    `--budget` needs it and adding the column later would be a migration."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    # ponytail: connection per operation. A run makes low hundreds of writes; if that ever
    # shows up in a profile, hold one connection open instead.
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def record(
        self,
        *,
        phase: str,
        model: str,
        usd: float,
        in_tok: int = 0,
        out_tok: int = 0,
        seconds: float = 0.0,
        run_id: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO spend (ts, run_id, phase, model, in_tok, out_tok, usd, seconds)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    run_id,
                    phase,
                    model,
                    in_tok,
                    out_tok,
                    usd,
                    seconds,
                ),
            )

    def _where(self, run_id: str | None) -> tuple[str, tuple[Any, ...]]:
        return ("", ()) if run_id is None else (" WHERE run_id = ?", (run_id,))

    def total(self, run_id: str | None = None) -> float:
        clause, params = self._where(run_id)
        with self._connect() as conn:
            row = conn.execute(f"SELECT COALESCE(SUM(usd), 0.0) AS t FROM spend{clause}", params)
            return float(row.fetchone()["t"])

    def _grouped(self, column: str, run_id: str | None) -> dict[str, float]:
        clause, params = self._where(run_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {column} AS k, SUM(usd) AS t FROM spend{clause}"
                f" GROUP BY {column} ORDER BY t DESC",
                params,
            ).fetchall()
        return {r["k"]: float(r["t"]) for r in rows}

    def by_phase(self, run_id: str | None = None) -> dict[str, float]:
        return self._grouped("phase", run_id)

    def by_model(self, run_id: str | None = None) -> dict[str, float]:
        return self._grouped("model", run_id)

    def rows(self, run_id: str | None = None) -> list[dict[str, Any]]:
        clause, params = self._where(run_id)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM spend{clause} ORDER BY id", params).fetchall()
        return [dict(r) for r in rows]
