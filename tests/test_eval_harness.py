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
