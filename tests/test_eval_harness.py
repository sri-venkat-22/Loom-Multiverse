"""WP-4.6 harness guards that run without a provider key or real model calls."""

from __future__ import annotations

from types import SimpleNamespace

import evals.harness as harness
from evals.harness import render_markdown, summarize_results
from loom.contracts import Response


def test_results_markdown_is_sorted_and_includes_required_metrics() -> None:
    """FR-EVAL-01 — the committed report has stable fixture rows and an aggregate row."""
    results = [
        {
            "id": "zebra",
            "passed": False,
            "score": 0.25,
            "turns": 8,
            "in_tok": 120,
            "out_tok": 40,
            "usd_spent": 0.0123,
            "wall_time": 3.5,
        },
        {
            "id": "alpha",
            "passed": True,
            "score": 1.0,
            "turns": 4,
            "in_tok": 80,
            "out_tok": 20,
            "usd_spent": 0.0045,
            "wall_time": 1.5,
        },
    ]

    summary = summarize_results(results, wall_time=5.0)
    report = render_markdown(results, summary)

    assert report.index("| alpha ") < report.index("| zebra ")
    assert "| **aggregate** | 50.0% (1/2) | 0.625 | 6.0 | 200 | 60 | 0.016800 | 5.0 |" in report
    assert "| fixture | pass | score | turns | in_tok | out_tok | usd | wall_s |" in report


def test_summary_totals_include_input_and_output_tokens() -> None:
    """FR-EVAL-01 — ledger token fields are reported, not inferred from turns."""
    summary = summarize_results(
        [
            {
                "id": "only",
                "passed": True,
                "score": 0.9,
                "turns": 2,
                "in_tok": 12,
                "out_tok": 7,
                "usd_spent": 0.02,
                "wall_time": 1.0,
            }
        ],
        wall_time=1.0,
    )

    assert summary["metrics"] == {
        "pass_rate": 1.0,
        "avg_rubric_score": 0.9,
        "avg_turns": 2.0,
        "total_in_tok": 12,
        "total_out_tok": 7,
        "total_usd_spent": 0.02,
        "wall_time": 1.0,
    }


def test_markdown_surfaces_error_rows_separately_from_bad_builds() -> None:
    """An infra failure (error set) must be distinguishable from a genuine 0-score build."""
    results = [
        {
            "id": "quota",
            "passed": False,
            "score": 0.0,
            "turns": 0,
            "in_tok": 0,
            "out_tok": 0,
            "usd_spent": 0.0,
            "wall_time": 1.0,
            "error": "RuntimeError: 429 daily quota exceeded",
        },
        {
            "id": "badbuild",
            "passed": False,
            "score": 0.1,
            "turns": 7,
            "in_tok": 90,
            "out_tok": 30,
            "usd_spent": 0.01,
            "wall_time": 2.0,
            "error": None,
        },
    ]
    report = render_markdown(results, summarize_results(results, wall_time=3.0))

    assert "429 daily quota exceeded" in report
    assert "**quota**" in report  # the errored fixture is called out by id
    assert "badbuild" not in report.split("## Errors")[1]  # a real 0-score build is not an error


async def test_fixture_captures_exception_string(monkeypatch, tmp_path) -> None:
    """A pipeline failure lands in the row as `error`, not just on stdout (FR-EVAL-01)."""

    class Stub:
        def __init__(self, *_a, **_k) -> None:
            pass

    async def boom(*_args, **_kwargs):
        raise RuntimeError("429 daily quota exceeded")

    monkeypatch.setattr(harness, "LiteLLMProvider", Stub)
    monkeypatch.setattr(harness, "run_pipeline", boom)

    result = await harness.run_fixture({"id": "quota", "idea": "an app"}, tmp_path)

    assert result["passed"] is False
    assert result["error"] == "RuntimeError: 429 daily quota exceeded"


async def test_fixture_totals_include_judge_provider_ledger_rows(monkeypatch, tmp_path) -> None:
    """FR-EVAL-01, FR-COST-02 — judge calls land in the fixture's own ledger."""

    class RecordingProvider:
        def __init__(self, _model, *, ledger, phase, run_id, **_kwargs) -> None:
            self.ledger = ledger
            self.phase = phase
            self.run_id = run_id

        async def complete(self, _messages, _tools) -> Response:
            response = Response(text='{"score": 1, "reason": "ok"}', in_tokens=5, out_tokens=7)
            self.ledger.record(
                phase=self.phase,
                model="fake",
                usd=0.03,
                in_tok=response.in_tokens,
                out_tok=response.out_tokens,
                run_id=self.run_id,
            )
            return response

    async def fake_pipeline(*_args, **kwargs):
        await kwargs["judge_provider"].complete([], None)
        return SimpleNamespace(
            status="passed",
            build=SimpleNamespace(turns_used=3, score=SimpleNamespace(total=0.8)),
        )

    monkeypatch.setattr(harness, "LiteLLMProvider", RecordingProvider)
    monkeypatch.setattr(harness, "run_pipeline", fake_pipeline)

    result = await harness.run_fixture({"id": "judge", "idea": "an app"}, tmp_path)

    assert result["in_tok"] == 5
    assert result["out_tok"] == 7
    assert result["usd_spent"] == 0.03
