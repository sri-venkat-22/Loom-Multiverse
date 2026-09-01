import asyncio
import json
import shutil
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from loom.agent.providers import LiteLLMProvider
from loom.agent.tools.ask_user import AskUser
from loom.cache import PhaseCache
from loom.config import MODEL_TIERS, Config, apply_credentials
from loom.ledger import Ledger
from loom.pipeline import run_pipeline
from loom.session import Session
from loom.workspace import Workspace

FIXTURES_PATH = Path(__file__).parent / "fixtures.json"
RESULTS_DIR = Path(__file__).parent / "results"

BUDGET = 1.25
MAX_TURNS = 40
MODEL = MODEL_TIERS["cheap"]
JUDGE_MODEL = MODEL_TIERS["cheap"]
MAX_CONCURRENCY = 4


async def run_fixture(fixture: Mapping[str, str], base_dir: Path) -> dict[str, Any]:
    idea = fixture["idea"]
    fid = fixture["id"]

    tmp_path = base_dir / fid
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    tmp_path.mkdir(parents=True)

    Workspace.create(tmp_path)
    ledger = Ledger(tmp_path / ".loom" / "ledger.db")
    session = Session(tmp_path, "e2e")
    events = []

    def record(kind: str, **fields: Any) -> None:
        events.append((kind, fields))
        session.log_event(kind, **fields)

    config = Config(
        model=MODEL,
        judge_model=JUDGE_MODEL,
        max_turns=MAX_TURNS,
        max_usd=BUDGET,
        budget_usd=BUDGET,
    )

    def provider_for(phase: str) -> LiteLLMProvider:
        return LiteLLMProvider(MODEL, ledger=ledger, phase=phase, run_id="e2e", on_event=record)

    start_time = time.monotonic()
    error: str | None = None
    try:
        result = await run_pipeline(
            idea,
            root=tmp_path,
            provider_factory=provider_for,
            config=config,
            # The rubric calls the judge provider directly. Binding its ledger here keeps judge
            # cost and tokens in the same per-fixture record, under the required separate phase.
            judge_provider=LiteLLMProvider(
                JUDGE_MODEL, ledger=ledger, phase="judge", run_id="e2e", on_event=record
            ),
            session=session,
            cache=PhaseCache(tmp_path, enabled=False),
            ask_user_fn=AskUser(yes=True, on_event=record),
            budget_usd=BUDGET,
        )
        passed = result.status == "passed"
    except Exception as e:
        passed = False
        result = None
        # Keep the reason in the row: a quota/network failure must not read as a bad build.
        error = f"{type(e).__name__}: {e}"
        print(f"[{fid}] Pipeline raised exception: {error}")

    wall_time = time.monotonic() - start_time

    rows = ledger.rows("e2e")
    in_tok = sum(row["in_tok"] for row in rows)
    out_tok = sum(row["out_tok"] for row in rows)
    usd_spent = ledger.total("e2e")
    turns = sum(1 for e in events if e[0] == "tool_call")
    if result and result.build:
        turns = result.build.turns_used
        score = result.build.score.total if result.build.score else 0.0
    else:
        score = 0.0

    return {
        "id": fid,
        "idea": idea,
        "passed": passed,
        "score": score,
        "turns": turns,
        "in_tok": in_tok,
        "out_tok": out_tok,
        "usd_spent": usd_spent,
        "wall_time": wall_time,
        "error": error,
    }


def summarize_results(results: Sequence[Mapping[str, Any]], *, wall_time: float) -> dict[str, Any]:
    """Aggregate every metric FR-EVAL-01 names, using totals where a sum is meaningful."""
    count = len(results)
    pass_count = sum(1 for result in results if result["passed"])
    return {
        "metrics": {
            "pass_rate": pass_count / count if count else 0.0,
            "avg_rubric_score": (
                sum(result["score"] for result in results) / count if count else 0.0
            ),
            "avg_turns": sum(result["turns"] for result in results) / count if count else 0.0,
            "total_in_tok": sum(result["in_tok"] for result in results),
            "total_out_tok": sum(result["out_tok"] for result in results),
            "total_usd_spent": sum(result["usd_spent"] for result in results),
            # This is elapsed harness time rather than sum of fixture time, so it remains
            # truthful when fixtures run concurrently.
            "wall_time": wall_time,
        },
        "runs": sorted((dict(result) for result in results), key=lambda result: result["id"]),
    }


def render_markdown(results: Sequence[Mapping[str, Any]], summary: Mapping[str, Any]) -> str:
    """A stable, human-readable report: fixture rows sorted by ID plus one aggregate row."""
    metrics = summary["metrics"]
    ordered = sorted(results, key=lambda result: result["id"])
    pass_count = sum(1 for result in ordered if result["passed"])
    lines = [
        "# Loom evaluation results",
        "",
        "| fixture | pass | score | turns | in_tok | out_tok | usd | wall_s |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in ordered:
        display = {**result, "passed": "yes" if result["passed"] else "no"}
        lines.append(
            "| {id} | {passed} | {score:.3f} | {turns} | {in_tok} | {out_tok} | "
            "{usd_spent:.6f} | {wall_time:.1f} |".format(**display)
        )
    lines.append(
        "| **aggregate** | {pass_rate:.1%} ({pass_count}/{count}) | "
        "{avg_rubric_score:.3f} | {avg_turns:.1f} | {total_in_tok} | {total_out_tok} | "
        "{total_usd_spent:.6f} | {wall_time:.1f} |".format(
            **metrics, pass_count=pass_count, count=len(ordered)
        )
    )
    # An errored fixture never reached a build. List those reasons apart from the score table so a
    # quota/network blip is not read as "Loom scored 0".
    errored = [result for result in ordered if result.get("error")]
    if errored:
        lines += ["", "## Errors", ""]
        lines += [f"- **{result['id']}**: {result['error']}" for result in errored]
    return "\n".join(lines) + "\n"


def _error_row(fixture: Mapping[str, str], exc: BaseException) -> dict[str, Any]:
    """A zeroed row for a fixture that failed before run_fixture could return one."""
    return {
        "id": fixture["id"],
        "idea": fixture.get("idea", ""),
        "passed": False,
        "score": 0.0,
        "turns": 0,
        "in_tok": 0,
        "out_tok": 0,
        "usd_spent": 0.0,
        "wall_time": 0.0,
        "error": f"{type(exc).__name__}: {exc}",
    }


async def _run_limited(
    fixture: Mapping[str, str], base_dir: Path, semaphore: asyncio.Semaphore
) -> dict[str, Any]:
    async with semaphore:
        print(f"-> Starting {fixture['id']} ...")
        result = await run_fixture(fixture, base_dir)
        print(
            f"<- {fixture['id']}: Pass={result['passed']}, Score={result['score']}, "
            f"USD=${result['usd_spent']:.3f}, Time={result['wall_time']:.1f}s"
        )
        return result


async def main() -> None:
    apply_credentials()  # FR-CFG-06 — NVIDIA_NIM_API_KEY from ~/.loom/credentials.json into env
    with FIXTURES_PATH.open(encoding="utf-8") as f:
        fixtures = json.load(f)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    base_dir = Path("/tmp/loom_evals")
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    print(f"Running eval harness on {len(fixtures)} fixtures...")
    start_time = time.monotonic()
    # return_exceptions: one fixture blowing up in setup must not wipe the whole report.
    raw = await asyncio.gather(
        *(_run_limited(fixture, base_dir, semaphore) for fixture in fixtures),
        return_exceptions=True,
    )
    results = [
        res if not isinstance(res, BaseException) else _error_row(fixture, res)
        for fixture, res in zip(fixtures, raw, strict=True)
    ]
    summary = summarize_results(results, wall_time=time.monotonic() - start_time)

    with (RESULTS_DIR / "report.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    (RESULTS_DIR / "results.md").write_text(render_markdown(results, summary), encoding="utf-8")

    metrics = summary["metrics"]
    print("\nEval Harness Completed.")
    print(f"Pass Rate: {metrics['pass_rate'] * 100:.1f}%")
    print(f"Average Score: {metrics['avg_rubric_score']:.3f}")
    print(f"Tokens: {metrics['total_in_tok']} in / {metrics['total_out_tok']} out")
    print(f"Total Spend: ${metrics['total_usd_spent']:.3f}")
    print(f"Wall Time: {metrics['wall_time']:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
