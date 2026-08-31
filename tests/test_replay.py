"""WP-3.7 — `loom replay`. FR-SESS-05.

The whole value of this command is the ratio: one provider call and a diff, instead of a full
pipeline, to see what an edit to a prompt did. Both halves of that are asserted here — the call
count, and that the previous artifact survives to be diffed against.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.cache import PhaseCache
from loom.config import Config
from loom.phases.base import artifact_path
from loom.pipeline import run_pipeline
from loom.replay import latest_artifact, next_slot, replay
from loom.session import Session
from loom.testing.fake_llm import FakeLLM
from tests.test_pipeline import DESIGN_OUT, PRD_OUT, VALIDATION, say


def config() -> Config:
    return Config(max_turns=4, max_usd=1.0, budget_usd=5.0)


@pytest.fixture
async def run(tmp_path: Path) -> tuple[Path, str]:
    """A finished validate→design run, which is what a replay needs on disk."""
    result = await run_pipeline(
        "a url shortener",
        root=tmp_path,
        provider_factory=lambda phase: FakeLLM([say(VALIDATION), say(PRD_OUT), say(DESIGN_OUT)]),
        config=config(),
        stop="design",
        cache=PhaseCache(tmp_path, enabled=False),
        fetcher=lambda url: (url, "<html>a page</html>"),
    )
    assert result.status == "passed"
    return tmp_path, result.run_id


MOVED = {**DESIGN_OUT, "summary": "a slugify library, rewritten"}


async def test_replaying_a_phase_costs_exactly_one_provider_call(run: tuple[Path, str]) -> None:
    """FR-SESS-05, and the reason the command exists."""
    root, run_id = run
    fake = FakeLLM([say(MOVED)])
    result = await replay(root=root, run_id=run_id, phase="design", provider=fake, config=config())
    assert fake.call_count == 1
    assert result.artifact.summary == "a slugify library, rewritten"


async def test_the_replay_reads_the_run_s_upstream_artifacts(run: tuple[Path, str]) -> None:
    root, run_id = run
    fake = FakeLLM([say(MOVED)])
    await replay(root=root, run_id=run_id, phase="design", provider=fake, config=config())
    assert "shorten a URL" in fake.calls[0][1]["content"]  # the PRD on disk


async def test_a_hand_edited_upstream_artifact_is_what_gets_replayed(
    run: tuple[Path, str],
) -> None:
    """FR-ART-03 — editing `prd.json` and replaying design is the main way this is used."""
    root, run_id = run
    path = artifact_path(root, run_id, "plan")
    path.write_text(json.dumps({**PRD_OUT, "v0_features": ["only one thing"]}), encoding="utf-8")
    fake = FakeLLM([say(MOVED)])
    await replay(root=root, run_id=run_id, phase="design", provider=fake, config=config())
    assert "only one thing" in fake.calls[0][1]["content"]


async def test_a_broken_upstream_artifact_is_refused_rather_than_half_read(
    run: tuple[Path, str],
) -> None:
    root, run_id = run
    artifact_path(root, run_id, "plan").write_text('{"product_name": "x"}', encoding="utf-8")
    with pytest.raises(Exception, match="users"):
        await replay(
            root=root, run_id=run_id, phase="design", provider=FakeLLM([]), config=config()
        )


# --------------------------------------------------------------------------- new slot


async def test_the_run_s_own_artifact_is_never_overwritten(run: tuple[Path, str]) -> None:
    """Comparing two prompts is worthless if running the second destroys the first's output."""
    root, run_id = run
    original = artifact_path(root, run_id, "design").read_text()
    result = await replay(
        root=root, run_id=run_id, phase="design", provider=FakeLLM([say(MOVED)]), config=config()
    )
    assert result.path.name == "design.replay-1.json"
    assert artifact_path(root, run_id, "design").read_text() == original
    assert json.loads(result.path.read_text())["summary"] == "a slugify library, rewritten"


async def test_slots_keep_counting_up(run: tuple[Path, str]) -> None:
    root, run_id = run
    for n in (1, 2, 3):
        result = await replay(
            root=root,
            run_id=run_id,
            phase="design",
            provider=FakeLLM([say({**MOVED, "summary": f"take {n}"})]),
            config=config(),
        )
        assert result.path.name == f"design.replay-{n}.json"


async def test_the_second_replay_diffs_against_the_first(run: tuple[Path, str]) -> None:
    """Replaying twice in a row should diff against the last thing you looked at."""
    root, run_id = run
    await replay(
        root=root,
        run_id=run_id,
        phase="design",
        provider=FakeLLM([say({**MOVED, "summary": "take one"})]),
        config=config(),
    )
    second = await replay(
        root=root,
        run_id=run_id,
        phase="design",
        provider=FakeLLM([say({**MOVED, "summary": "take two"})]),
        config=config(),
    )
    assert second.previous_path.name == "design.replay-1.json"
    assert "take one" in second.diff and "take two" in second.diff


def test_slot_numbering_survives_a_gap(tmp_path: Path) -> None:
    """Someone deletes `design.replay-2.json`; the next one must not reuse the number and
    silently overwrite a comparison they still have open."""
    base = artifact_path(tmp_path, "r1", "design")
    base.parent.mkdir(parents=True)
    base.write_text("{}")
    for n in (1, 3):
        base.with_name(f"design.replay-{n}.json").write_text("{}")
    assert next_slot(tmp_path, "r1", "design").name == "design.replay-4.json"
    assert latest_artifact(tmp_path, "r1", "design").name == "design.replay-3.json"


# --------------------------------------------------------------------------- the diff


async def test_the_result_carries_a_readable_diff(run: tuple[Path, str]) -> None:
    root, run_id = run
    result = await replay(
        root=root, run_id=run_id, phase="design", provider=FakeLLM([say(MOVED)]), config=config()
    )
    assert "-" in result.diff and "+" in result.diff
    assert "a slugify library, rewritten" in result.diff
    assert "design (previous)" in result.diff


async def test_an_unchanged_answer_says_so(run: tuple[Path, str]) -> None:
    root, run_id = run
    result = await replay(
        root=root,
        run_id=run_id,
        phase="design",
        provider=FakeLLM([say(DESIGN_OUT)]),
        config=config(),
    )
    assert "byte-identical" in result.diff


# --------------------------------------------------------------------------- the prompt


async def test_an_alternative_prompt_file_is_what_reaches_the_model(
    run: tuple[Path, str], tmp_path: Path
) -> None:
    """The command this whole module exists for: try a prompt, diff the result."""
    root, run_id = run
    candidate = tmp_path / "design-v2.md"
    candidate.write_text("Design it, but in Rust this time.", encoding="utf-8")
    fake = FakeLLM([say(MOVED)])
    result = await replay(
        root=root,
        run_id=run_id,
        phase="design",
        provider=fake,
        config=config(),
        prompt_path=candidate,
    )
    assert "in Rust this time" in fake.calls[0][0]["content"]
    assert result.prompt_path == candidate


async def test_the_cache_is_bypassed(tmp_path: Path) -> None:
    """An unchanged prompt would otherwise serve the old answer and make zero calls, which is
    the exact opposite of what a replay is for. The cache is warm here on purpose."""
    warm = PhaseCache(tmp_path)
    first = await run_pipeline(
        "a url shortener",
        root=tmp_path,
        provider_factory=lambda phase: FakeLLM([say(VALIDATION), say(PRD_OUT), say(DESIGN_OUT)]),
        config=config(),
        stop="design",
        cache=warm,
        fetcher=lambda url: (url, "<html>a page</html>"),
    )
    assert warm.dir.is_dir() and any(warm.dir.iterdir())

    fake = FakeLLM([say(DESIGN_OUT)])
    await replay(root=tmp_path, run_id=first.run_id, phase="design", provider=fake, config=config())
    assert fake.call_count == 1


# --------------------------------------------------------------------------- refusals


async def test_replaying_the_build_is_refused_with_a_reason(run: tuple[Path, str]) -> None:
    root, run_id = run
    with pytest.raises(ValueError, match="resetting the workspace"):
        await replay(root=root, run_id=run_id, phase="build", provider=FakeLLM([]), config=config())


async def test_replaying_a_phase_the_run_never_reached_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="nothing to"):
        await replay(
            root=tmp_path, run_id="r1", phase="design", provider=FakeLLM([]), config=config()
        )


async def test_the_replay_is_recorded_in_the_run_s_events(run: tuple[Path, str]) -> None:
    """A file appearing in `.loom/artifacts/` with no event explaining it is a mystery in three
    weeks' time."""
    root, run_id = run
    await replay(
        root=root, run_id=run_id, phase="design", provider=FakeLLM([say(MOVED)]), config=config()
    )
    written = [
        e
        for e in Session(root, run_id).read_events()
        if e["kind"] == "artifact_written" and e.get("replay")
    ]
    assert len(written) == 1
    assert written[0]["path"].endswith("design.replay-1.json")


async def test_replaying_validate_finds_the_idea_in_the_run(run: tuple[Path, str]) -> None:
    """Nobody should have to re-type the idea to replay the phase that researched it."""
    root, run_id = run
    fake = FakeLLM([say(VALIDATION)])
    await replay(
        root=root,
        run_id=run_id,
        phase="validate",
        provider=fake,
        config=config(),
    )
    assert "a url shortener" in fake.calls[0][1]["content"]


async def test_replay_events_do_not_disturb_the_run_s_sequence(run: tuple[Path, str]) -> None:
    """The replay writes into the same event log; `seq` must stay monotonic or `loom status`
    reads the run backwards."""
    root, run_id = run
    before = len(Session(root, run_id).read_events())
    await replay(
        root=root, run_id=run_id, phase="design", provider=FakeLLM([say(MOVED)]), config=config()
    )
    events = Session(root, run_id).read_events()
    assert len(events) > before
    assert [e["seq"] for e in events] == list(range(len(events)))
