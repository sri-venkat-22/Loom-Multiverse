"""WP-8.2 — the REPL: input classification, routing by run state, the four prefixes, and the
interactive loop driven against `prompt_toolkit` pipe input (never screen-scraped).

FR-REPL-01 (submit + return), FR-REPL-02 (hint bar), FR-REPL-03 (routing, one case per state),
FR-REPL-04 (empty is a no-op), FR-REPL-07 (multi-line), FR-REPL-08 (`@` stays in the jail),
FR-REPL-09 (`!` through the agent's guard), FR-REPL-10 (`#` notes reach the phase), FR-KEY-02
(shift-tab cycles modes), FR-SLASH-03 (the palette completer).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic import BaseModel

from loom.config import load_config
from loom.contracts import Response
from loom.phases.base import Phase
from loom.security import CommandDenied, check_command
from loom.session import append_note, notes_path, read_notes
from loom.testing.fake_llm import FakeLLM
from loom.tui.complete import PathCompleter, SlashCompleter
from loom.tui.repl import (
    MODES,
    Repl,
    ReplActions,
    classify_input,
    next_mode,
    route_free_text,
    run_bash_line,
)


def _repl(tmp_path: Path, **actions: object) -> tuple[Repl, list[str]]:
    (tmp_path / ".loom").mkdir(exist_ok=True)
    writes: list[str] = []
    defaults: dict[str, object] = {"run_state": lambda: "idle"}
    defaults.update(actions)
    repl = Repl(
        root=tmp_path,
        config=load_config(cwd=tmp_path),
        actions=ReplActions(**defaults),  # type: ignore[arg-type]
        out=writes.append,
    )
    return repl, writes


# --------------------------------------------------------------------------- FR-REPL-03 / 04


@pytest.mark.parametrize(
    "state,kind",
    [
        ("idle", "new_idea"),
        ("gate", "gate_feedback"),
        ("running", "queue"),
        ("finished", "followup"),
    ],
)
def test_free_text_routes_by_run_state(state: str, kind: str) -> None:
    route = route_free_text(state, "make a todo app", gate_phase="design")  # type: ignore[arg-type]
    assert route.kind == kind
    assert route.line.strip(), "the route must be stated in one line before work begins"
    assert route.payload == "make a todo app"


def test_gate_route_names_the_phase_under_review() -> None:
    assert "design" in route_free_text("gate", "fix it", gate_phase="design").line


def test_empty_input_is_a_noop() -> None:
    assert classify_input("")[0] == "empty"
    assert classify_input("   \t ")[0] == "empty"
    assert route_free_text("idle", "   ").kind == "noop"


def test_whitespace_does_not_start_a_run(tmp_path: Path) -> None:
    got: list[str] = []
    repl, _ = _repl(tmp_path, start_idea=got.append)
    repl.handle("    ")
    assert got == []


@pytest.mark.parametrize(
    "text,kind",
    [("/model", "slash"), ("!ls", "bash"), ("# remember", "note"), ("just do it", "text")],
)
def test_classify_input(text: str, kind: str) -> None:
    assert classify_input(text)[0] == kind


def test_handle_routes_free_text_to_the_state_action(tmp_path: Path) -> None:
    got: list[str] = []
    repl, writes = _repl(tmp_path, start_idea=got.append, run_state=lambda: "idle")
    repl.handle("build a url shortener")
    assert got == ["build a url shortener"]
    assert any(w.startswith("→") for w in writes)  # the stated route


# --------------------------------------------------------------------------- FR-REPL-09 (`!`)


def test_bang_is_denied_by_the_same_guard_as_the_agent(tmp_path: Path) -> None:
    (tmp_path / ".loom").mkdir()
    output = run_bash_line(tmp_path, "rm -rf /")
    assert "denied" in output  # the deny reason is shown
    # ...and it is the *same* code path the agent's run_bash calls.
    with pytest.raises(CommandDenied):
        check_command("rm -rf /", root=tmp_path)


def test_bang_runs_an_allowed_command_in_the_workspace(tmp_path: Path) -> None:
    output = run_bash_line(tmp_path, "echo hello")
    assert "hello" in output


def test_handle_bang_writes_the_guarded_result(tmp_path: Path) -> None:
    repl, writes = _repl(tmp_path)
    repl.handle("!rm -rf /")
    assert any("denied" in w for w in writes)


# --------------------------------------------------------------------------- FR-REPL-10 (`#`)


def test_hash_appends_a_note(tmp_path: Path) -> None:
    repl, writes = _repl(tmp_path)
    repl.handle("# keep it dependency-free")
    assert notes_path(tmp_path).is_file()
    assert "keep it dependency-free" in read_notes(tmp_path)
    assert any("noted" in w for w in writes)


def test_note_is_injected_into_the_next_phase_system_message(tmp_path: Path) -> None:
    (tmp_path / ".loom").mkdir()
    append_note(tmp_path, "no external services")

    class Tiny(BaseModel):
        x: int

    class TinyPhase(Phase):
        name = "validate"
        artifact_model = Tiny
        prompt_name = "validate"

        def prepare_input(self, **inputs: object) -> str:
            return "do the thing"

    prompt = tmp_path / "p.md"
    prompt.write_text("PROMPT", encoding="utf-8")
    fake = FakeLLM([Response(text='{"x": 1}')] * 3)
    asyncio.run(TinyPhase(prompt_path=prompt).execute(provider=fake, root=tmp_path, run_id="r"))

    system = fake.calls[0][0]["content"]
    assert "no external services" in system  # FR-REPL-10 — the note reached the phase
    assert "User constraints" in system  # under the delimited heading


# --------------------------------------------------------------------------- FR-REPL-08 (`@`)


def test_path_completion_stays_in_the_jail_and_hides_git_and_loom(tmp_path: Path) -> None:
    (tmp_path / ".loom").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    completer = PathCompleter(tmp_path)
    offered = [c.text for c in completer.get_completions(Document("@", 1), None)]
    assert "src/" in offered and "README.md" in offered
    assert ".git/" not in offered and ".loom/" not in offered


def test_path_completion_filters_by_prefix(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    (tmp_path / "other.py").write_text("x", encoding="utf-8")
    completer = PathCompleter(tmp_path)
    offered = [c.text for c in completer.get_completions(Document("@ap", 3), None)]
    assert offered == ["app.py"]


# --------------------------------------------------------------------------- FR-SLASH-03 palette


def test_slash_completer_offers_the_filtered_registry() -> None:
    completer = SlashCompleter()
    offered = [c.text for c in completer.get_completions(Document("/mod", 4), None)]
    assert "/model" in offered
    # a description-carrying meta comes along for the menu
    metas = [c.display_meta_text for c in completer.get_completions(Document("/model", 6), None)]
    assert any("Switch model" in m for m in metas)


# --------------------------------------------------------------------------- FR-REPL-02 / KEY-02


def test_hint_bar_shows_mode_and_prefixes_and_updates(tmp_path: Path) -> None:
    repl, _ = _repl(tmp_path)
    hint = repl._hint()
    assert "auto mode" in hint
    for prefix in ("@ for files", "! for bash", "# to remember"):
        assert prefix in hint
    repl.mode = "strict"
    assert "strict mode" in repl._hint()  # updates immediately when the mode changes


def test_mode_cycle_rotates_through_the_four_modes() -> None:
    assert list(MODES) == ["auto", "strict", "plan-only", "unattended"]
    assert next_mode("auto") == "strict"
    assert next_mode("unattended") == "auto"


# --------------------------------------------------------------------------- FR-REPL-01 / 07 (pipe)


def test_prompt_submits_on_enter_and_returns_to_the_prompt(tmp_path: Path) -> None:
    got: list[str] = []
    repl, _ = _repl(tmp_path, start_idea=got.append)
    with create_pipe_input() as pin:
        pin.send_text("make a todo app\r")  # submit
        pin.send_text("\x04")  # Ctrl-D on the next, empty prompt exits
        code = repl.run(None, input=pin, output=DummyOutput())
    assert code == 0
    assert got == ["make a todo app"]  # one line in, one submission, back to the prompt


def test_backslash_continuation_makes_one_multiline_submission(tmp_path: Path) -> None:
    got: list[str] = []
    repl, _ = _repl(tmp_path, start_idea=got.append)
    with create_pipe_input() as pin:
        pin.send_text("first\\\r")  # trailing backslash → newline, not submit (FR-REPL-07)
        pin.send_text("second\r")  # now submit
        pin.send_text("\x04")
        repl.run(None, input=pin, output=DummyOutput())
    assert got == ["first\nsecond"]


def test_ctrl_d_exits_zero(tmp_path: Path) -> None:
    repl, _ = _repl(tmp_path)
    with create_pipe_input() as pin:
        pin.send_text("\x04")
        assert repl.run(None, input=pin, output=DummyOutput()) == 0
