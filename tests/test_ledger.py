from __future__ import annotations

from pathlib import Path

import pytest

from loom.ledger import Ledger


@pytest.fixture()
def ledger(tmp_path: Path) -> Ledger:
    led = Ledger(tmp_path / "nested" / "ledger.db")
    led.record(phase="validate", model="qwen", usd=0.10, in_tok=1000, out_tok=100, run_id="r1")
    led.record(phase="build", model="qwen", usd=0.50, in_tok=9000, out_tok=800, run_id="r1")
    led.record(phase="judge", model="cheap", usd=0.02, run_id="r1")
    led.record(phase="build", model="sonnet", usd=1.00, run_id="r2")
    return led


def test_it_creates_its_own_directory(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "a" / "b" / "ledger.db")
    assert led.path.exists()
    assert led.total() == 0.0 and led.by_phase() == {} and led.rows() == []


def test_grand_total(ledger: Ledger) -> None:
    assert ledger.total() == pytest.approx(1.62)


def test_totals_by_phase(ledger: Ledger) -> None:
    assert ledger.by_phase() == pytest.approx({"build": 1.50, "validate": 0.10, "judge": 0.02})


def test_totals_by_model(ledger: Ledger) -> None:
    assert ledger.by_model() == pytest.approx({"sonnet": 1.00, "qwen": 0.60, "cheap": 0.02})


def test_everything_filters_by_run(ledger: Ledger) -> None:
    assert ledger.total("r1") == pytest.approx(0.62)
    assert ledger.by_phase("r1") == pytest.approx({"build": 0.50, "validate": 0.10, "judge": 0.02})
    assert ledger.by_model("r2") == pytest.approx({"sonnet": 1.00})
    assert ledger.total("nonexistent") == 0.0


def test_judge_spend_is_separable_from_the_phase_that_triggered_it(ledger: Ledger) -> None:
    assert ledger.by_phase("r1")["judge"] == pytest.approx(0.02)
    assert "judge" not in ledger.by_model("r1")


def test_rows_round_trip_the_columns(ledger: Ledger) -> None:
    rows = ledger.rows("r1")
    assert [r["phase"] for r in rows] == ["validate", "build", "judge"]
    assert rows[1]["in_tok"] == 9000 and rows[1]["out_tok"] == 800
    assert rows[2]["seconds"] == 0.0 and rows[2]["ts"]


def test_it_survives_reopening_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    Ledger(path).record(phase="build", model="qwen", usd=0.25)
    reopened = Ledger(path)
    reopened.record(phase="build", model="qwen", usd=0.25, seconds=3.5)
    assert reopened.total() == pytest.approx(0.50)
    assert len(reopened.rows()) == 2
