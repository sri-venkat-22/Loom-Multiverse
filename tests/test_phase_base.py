"""WP-3.1 — the Shape A machine. FR-PIPE-05/06, FR-ART-01, FR-ART-03, SEC-03.

The repair loop is the reason this file exists. A model that returns prose around its JSON, or
JSON missing a field, is the normal case rather than the exception, and the difference between
paying one call for it and paying a whole re-run is the whole cost model of the project.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, Field

from loom.cache import PhaseCache
from loom.contracts import Response, ToolCall
from loom.phases.base import (
    MAX_ATTEMPTS,
    Phase,
    PhaseError,
    artifact_path,
    load_artifact,
)
from loom.testing.fake_llm import FakeLLM


class Note(BaseModel):
    """A three-line artifact, so the tests are about the machine and not about a contract."""

    title: str
    tags: list[str] = Field(default_factory=list)


class NotePhase(Phase):
    name: ClassVar[str] = "plan"  # borrows plan's artifact slot; the model is what is under test
    artifact_model: ClassVar[type] = Note
    prompt_name: ClassVar[str] = "plan"

    def prepare_input(self, *, subject: str = "", **_: Any) -> str:
        return f"Write a note about {subject}."


def say(text: str) -> Response:
    return Response(text=text, usd_cost=0.001)


VALID = say('{"title": "a note", "tags": ["x"]}')


async def test_a_clean_answer_costs_exactly_one_call(tmp_path: Path) -> None:
    """The happy path is one provider call. FR-SESS-05 depends on this being true."""
    fake = FakeLLM([VALID])
    outcome = await NotePhase().execute(
        provider=fake, inputs={"subject": "x"}, root=tmp_path, run_id="r1"
    )
    assert fake.call_count == 1
    assert outcome.artifact.title == "a note"
    assert outcome.attempts == 1


async def test_prose_around_the_json_is_not_a_failure(tmp_path: Path) -> None:
    """A fenced block or a paragraph of throat-clearing costs nothing — making the model pay a
    repair round for our parser would be our bug, not its."""
    fake = FakeLLM([say('Sure!\n```json\n{"title": "a note"}\n```\nHope that helps.')])
    outcome = await NotePhase().execute(provider=fake, inputs={"subject": "x"}, root=tmp_path)
    assert fake.call_count == 1
    assert outcome.artifact.title == "a note"


async def test_invalid_twice_then_valid_succeeds_on_the_third_attempt(tmp_path: Path) -> None:
    """FR-PIPE-06, verbatim."""
    fake = FakeLLM([say("no json here"), say('{"tags": []}'), VALID])
    outcome = await NotePhase().execute(
        provider=fake, inputs={"subject": "x"}, root=tmp_path, run_id="r1"
    )
    assert outcome.attempts == MAX_ATTEMPTS
    assert fake.call_count == 3
    assert outcome.artifact.title == "a note"


async def test_the_repair_message_carries_pydantic_s_own_error(tmp_path: Path) -> None:
    """FR-BUILD-03's sibling: "invalid" is not feedback, the missing field name is."""
    fake = FakeLLM([say('{"tags": []}'), VALID])
    await NotePhase().execute(provider=fake, inputs={"subject": "x"}, root=tmp_path)
    repair = fake.calls[1][-1]
    assert repair["role"] == "user"
    assert "title" in repair["content"]
    assert "Field required" in repair["content"]


async def test_three_failures_raise_a_phase_error_carrying_all_three(tmp_path: Path) -> None:
    fake = FakeLLM([say("one"), say("two"), say("three")])
    with pytest.raises(PhaseError) as caught:
        await NotePhase().execute(provider=fake, inputs={"subject": "x"}, root=tmp_path)
    assert len(caught.value.attempts) == MAX_ATTEMPTS
    assert [a.raw for a in caught.value.attempts] == ["one", "two", "three"]
    assert "one" in str(caught.value) and "three" in str(caught.value)


async def test_a_failed_phase_writes_no_artifact(tmp_path: Path) -> None:
    """FR-ART-01 — after validation, never before. A half-run must leave nothing to read."""
    fake = FakeLLM([say("a"), say("b"), say("c")])
    with pytest.raises(PhaseError):
        await NotePhase().execute(
            provider=fake, inputs={"subject": "x"}, root=tmp_path, run_id="r1"
        )
    assert not artifact_path(tmp_path, "r1", "plan").exists()


async def test_the_artifact_lands_where_the_srs_says(tmp_path: Path) -> None:
    outcome = await NotePhase().execute(
        provider=FakeLLM([VALID]), inputs={"subject": "x"}, root=tmp_path, run_id="r1"
    )
    assert outcome.path == tmp_path / ".loom" / "artifacts" / "r1" / "prd.json"
    assert json.loads(outcome.path.read_text())["title"] == "a note"


async def test_persist_false_leaves_the_run_s_artifact_alone(tmp_path: Path) -> None:
    """What `loom replay` needs: produce the artifact, do not overwrite the run's."""
    outcome = await NotePhase().execute(
        provider=FakeLLM([VALID]),
        inputs={"subject": "x"},
        root=tmp_path,
        run_id="r1",
        persist=False,
    )
    assert outcome.path is None
    assert not artifact_path(tmp_path, "r1", "plan").exists()


def test_a_hand_edited_artifact_is_re_validated_on_the_way_back_in(tmp_path: Path) -> None:
    """FR-ART-03 — the file on disk is a human's to edit, and a broken edit must not reach a
    downstream phase as a half-valid object."""
    path = artifact_path(tmp_path, "r1", "plan")
    path.parent.mkdir(parents=True)
    path.write_text('{"tags": ["x"]}', encoding="utf-8")
    with pytest.raises(Exception, match="title"):
        load_artifact(tmp_path, "r1", "plan", Note)


def test_a_missing_artifact_says_which_phase_to_run(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Run that phase first"):
        load_artifact(tmp_path, "r1", "plan", Note)


# --------------------------------------------------------------------------- SEC-03


async def test_a_shape_a_phase_is_offered_no_filesystem_and_no_bash_tool(tmp_path: Path) -> None:
    """SEC-03 — asserted on the list actually sent to the provider, not on the construction."""
    fake = FakeLLM([VALID])
    await NotePhase().execute(provider=fake, inputs={"subject": "x"}, root=tmp_path)
    offered = set(fake.tool_names_seen(0))
    assert offered & {"read_file", "write_file", "str_replace", "run_bash"} == set()
    assert "ask_user" in offered


async def test_the_schema_reaches_the_model(tmp_path: Path) -> None:
    """The prompt file and the output model cannot drift: the schema is generated, not typed."""
    fake = FakeLLM([VALID])
    await NotePhase().execute(provider=fake, inputs={"subject": "x"}, root=tmp_path)
    system = fake.calls[0][0]["content"]
    assert '"title"' in system and "JSON object" in system


# --------------------------------------------------------------------------- caching


async def test_a_second_identical_run_makes_no_provider_call(tmp_path: Path) -> None:
    """FR-PIPE-05, asserted the only way that means anything: call count."""
    cache = PhaseCache(tmp_path)
    first = FakeLLM([VALID])
    await NotePhase().execute(provider=first, inputs={"subject": "x"}, root=tmp_path, cache=cache)
    second = FakeLLM([])  # a single call would raise ScriptExhausted
    outcome = await NotePhase().execute(
        provider=second, inputs={"subject": "x"}, root=tmp_path, cache=cache
    )
    assert second.call_count == 0
    assert outcome.cached is True
    assert outcome.artifact.title == "a note"


async def test_one_byte_of_the_prompt_busts_the_cache(tmp_path: Path, prompt: Path) -> None:
    cache = PhaseCache(tmp_path)
    phase = NotePhase(prompt_path=prompt)
    await phase.execute(provider=FakeLLM([VALID]), inputs={"subject": "x"}, cache=cache)

    prompt.write_text(prompt.read_text() + ".", encoding="utf-8")
    after = FakeLLM([VALID])
    await NotePhase(prompt_path=prompt).execute(
        provider=after, inputs={"subject": "x"}, cache=cache
    )
    assert after.call_count == 1


async def test_no_cache_bypasses_it(tmp_path: Path) -> None:
    warm = PhaseCache(tmp_path)
    await NotePhase().execute(provider=FakeLLM([VALID]), inputs={"subject": "x"}, cache=warm)
    cold = FakeLLM([VALID])
    await NotePhase().execute(
        provider=cold, inputs={"subject": "x"}, cache=PhaseCache(tmp_path, enabled=False)
    )
    assert cold.call_count == 1


async def test_the_cache_still_persists_the_artifact(tmp_path: Path) -> None:
    """A cache hit that skipped writing `prd.json` would leave the next phase with nothing to
    read — the cache saves a call, not a file."""
    cache = PhaseCache(tmp_path)
    await NotePhase().execute(
        provider=FakeLLM([VALID]),
        inputs={"subject": "x"},
        root=tmp_path,
        run_id="r1",
        cache=cache,
    )
    artifact_path(tmp_path, "r1", "plan").unlink()
    await NotePhase().execute(
        provider=FakeLLM([]), inputs={"subject": "x"}, root=tmp_path, run_id="r1", cache=cache
    )
    assert artifact_path(tmp_path, "r1", "plan").is_file()


# --------------------------------------------------------------------------- events and money


async def test_the_phase_narrates_itself(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    fake = FakeLLM([say("nope"), VALID])
    await NotePhase().execute(
        provider=fake,
        inputs={"subject": "x"},
        root=tmp_path,
        run_id="r1",
        on_event=lambda kind, **f: events.append((kind, f)),
    )
    kinds = [k for k, _ in events]
    assert kinds[0] == "phase_started"
    assert "retry" in kinds  # the repair is visible, not silent
    assert "artifact_written" in kinds
    assert kinds[-1] == "phase_finished"


async def test_a_repair_is_charged_for(tmp_path: Path) -> None:
    """The repair call costs money and the phase must say so, or a run's spend is under-reported
    by exactly the amount our own parser cost."""
    outcome = await NotePhase().execute(
        provider=FakeLLM([say("nope"), VALID]), inputs={"subject": "x"}, root=tmp_path
    )
    assert outcome.usd_spent == pytest.approx(0.002)


async def test_reviewer_feedback_reaches_the_task_not_the_transcript(tmp_path: Path) -> None:
    """FR-GATE-02 — a rejected phase re-runs with the feedback in its input, and without its
    own rejected artifact to anchor on."""
    fake = FakeLLM([VALID])
    await NotePhase().execute(
        provider=fake, inputs={"subject": "x"}, root=tmp_path, feedback="too vague"
    )
    task = fake.calls[0][1]["content"]
    assert "too vague" in task
    assert '"title"' not in task  # the rejected artifact itself is not fed back


async def test_a_tool_call_does_not_confuse_the_parser(tmp_path: Path) -> None:
    """The artifact comes from the final assistant message, not from whatever was said before
    a tool call."""
    fake = FakeLLM(
        [
            Response(
                text='{"title": "not this one"}',
                tool_calls=[ToolCall(id="c1", name="ask_user", arguments={"question": "?"})],
            ),
            VALID,
        ]
    )
    outcome = await NotePhase().execute(
        provider=fake,
        inputs={"subject": "x"},
        root=tmp_path,
        ask_user_fn=lambda **_: "yes",
    )
    assert outcome.artifact.title == "a note"


@pytest.fixture
def prompt(tmp_path: Path) -> Path:
    path = tmp_path / "note.md"
    path.write_text("Write a note.", encoding="utf-8")
    return path
