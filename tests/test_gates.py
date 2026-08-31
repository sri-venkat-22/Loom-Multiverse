"""WP-3.5 — approval gates. FR-GATE-01/02/03/04/06, FR-ART-03.

Every reviewer here is scripted and every editor is a function, so the whole interactive
surface is tested with no TTY. That is not a testing convenience — it is the same property
`--yes` relies on, checked from the other side.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from loom.contracts import PRD
from loom.gates import AutoApprove, Decision, TerminalGate, diff, edit_artifact, render

PRD_A = PRD(
    product_name="Snip",
    problem="links are opaque",
    users=["marketers"],
    v0_features=["shorten", "resolve"],
)
PRD_B = PRD_A.model_copy(update={"product_name": "Snipper"})


class Reviewer:
    """A scripted human. Runs out loudly rather than hanging on a prompt that never comes."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []
        self.shown: list[str] = []

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise AssertionError(f"the gate asked one question too many: {prompt!r}")
        return self.answers.pop(0)

    def write(self, text: str) -> None:
        self.shown.append(text)

    @property
    def screen(self) -> str:
        return "\n".join(self.shown)


def gate(
    *answers: str, editor: Callable[[Path], None] | None = None
) -> tuple[TerminalGate, Reviewer]:
    reviewer = Reviewer(*answers)
    events: list[tuple[str, dict[str, Any]]] = []
    g = TerminalGate(
        ask=reviewer.ask,
        write=reviewer.write,
        editor=editor or (lambda path: None),
        on_event=lambda kind, **f: events.append((kind, f)),
    )
    g.events = events  # type: ignore[attr-defined]
    return g, reviewer


# --------------------------------------------------------------------------- FR-GATE-01


def test_approve_reject_and_edit_are_all_offered() -> None:
    g, reviewer = gate("a")
    g("plan", PRD_A)
    prompt = reviewer.asked[0].replace("[", "").replace("]", "")
    for word in ("approve", "reject", "edit", "quit"):
        assert word in prompt


def test_approve_is_the_decision_and_the_event() -> None:
    g, _ = gate("a")
    assert g("plan", PRD_A) == Decision(action="approve")
    kinds = [k for k, _ in g.events]  # type: ignore[attr-defined]
    assert kinds == ["gate_opened", "gate_decision"]


def test_the_artifact_is_shown_before_the_question() -> None:
    g, reviewer = gate("a")
    g("plan", PRD_A)
    assert "Snip" in reviewer.screen
    assert "shorten" in reviewer.screen


def test_an_unrecognised_key_re_asks_rather_than_guessing() -> None:
    """Guessing "approve" from a typo is the one wrong default at a gate."""
    g, reviewer = gate("wat", "a")
    assert g("plan", PRD_A).action == "approve"
    assert "Not one of the options." in reviewer.screen


def test_enter_approves() -> None:
    """The common case is approval, and the reviewer has just read the artifact."""
    g, _ = gate("")
    assert g("plan", PRD_A).action == "approve"


# --------------------------------------------------------------------------- FR-GATE-02


def test_reject_carries_the_feedback_and_asks_for_a_re_run() -> None:
    g, _ = gate("r", "the problem statement names no one")
    decision = g("plan", PRD_A)
    assert decision.action == "reject"
    assert decision.feedback == "the problem statement names no one"
    assert decision.reruns is True


def test_an_empty_rejection_is_refused() -> None:
    """Re-running a phase with no feedback buys the same artifact with different adjectives."""
    g, reviewer = gate("r", "", "r", "too vague")
    assert g("plan", PRD_A).feedback == "too vague"
    assert "Say what to change." in reviewer.screen


def test_edit_returns_the_human_s_version_and_does_not_re_run() -> None:
    def editor(path: Path) -> None:
        path.write_text(render(PRD_B), encoding="utf-8")

    g, _ = gate("e", editor=editor)
    decision = g("plan", PRD_A)
    assert decision.action == "edit"
    assert decision.artifact == PRD_B
    assert decision.reruns is False


def test_quit_abandons_the_run() -> None:
    g, _ = gate("q")
    assert g("plan", PRD_A).action == "abandon"


# --------------------------------------------------------------------------- FR-GATE-03


def test_a_re_run_is_reviewed_as_a_diff() -> None:
    """Reviewing the second attempt on its own merits is how a re-run silently drops the one
    section that was right the first time."""
    out = diff(PRD_A, PRD_B, phase="plan")
    assert '-  "product_name": "Snip"' in out
    assert '+  "product_name": "Snipper"' in out
    assert "plan (previous)" in out


def test_a_first_attempt_is_shown_whole() -> None:
    assert diff(None, PRD_A) == render(PRD_A)


def test_an_identical_re_run_says_so_rather_than_showing_nothing() -> None:
    """An empty diff on screen reads as a rendering bug, not as "the model did not move"."""
    assert "byte-identical" in diff(PRD_A, PRD_A, phase="plan")


def test_the_gate_shows_the_diff_when_there_is_a_previous() -> None:
    g, reviewer = gate("a")
    g("plan", PRD_B, PRD_A)
    assert "Snipper" in reviewer.screen
    assert "previous" in reviewer.screen


# --------------------------------------------------------------------------- FR-GATE-04


def test_auto_approve_approves_everything_and_logs_each_one() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    auto = AutoApprove(on_event=lambda kind, **f: events.append((kind, f)))
    for phase in ("validate", "plan", "design", "build"):
        assert auto(phase, PRD_A).action == "approve"
    assert auto.seen == ["validate", "plan", "design", "build"]
    decisions = [f for k, f in events if k == "gate_decision"]
    assert len(decisions) == 4
    assert all(f["auto"] is True for f in decisions)


def test_auto_approve_needs_no_terminal_at_all() -> None:
    """FR-HEADLESS-01 — no `ask`, no `write`, nothing to attach to a TTY."""
    assert AutoApprove()("plan", PRD_A).action == "approve"


# --------------------------------------------------------------------------- FR-GATE-06


def test_an_invalid_edit_reopens_the_editor_with_the_error() -> None:
    attempts = []

    def editor(path: Path) -> None:
        attempts.append(path.read_text())
        path.write_text('{"product_name": "Snip"}' if len(attempts) == 1 else render(PRD_B))

    reviewer = Reviewer("y")
    edited = edit_artifact(PRD_A, editor=editor, write=reviewer.write, ask=reviewer.ask)
    assert edited == PRD_B
    assert len(attempts) == 2
    assert "users" in reviewer.screen  # the missing field is named


def test_giving_up_on_an_edit_leaves_the_artifact_alone() -> None:
    """Saving the file unchanged after a failed validation is what someone does when they
    cannot see how to fix it. It must not become a third prompt."""

    def editor(path: Path) -> None:
        path.write_text("{}", encoding="utf-8")

    reviewer = Reviewer("y")
    assert edit_artifact(PRD_A, editor=editor, write=reviewer.write, ask=reviewer.ask) is None


def test_declining_to_reopen_leaves_the_artifact_alone() -> None:
    def editor(path: Path) -> None:
        path.write_text("not json at all", encoding="utf-8")

    reviewer = Reviewer("n")
    assert edit_artifact(PRD_A, editor=editor, write=reviewer.write, ask=reviewer.ask) is None


def test_the_editor_never_sees_the_real_artifact_file(tmp_path: Path) -> None:
    """FR-ART-03 — a copy, so an abandoned edit cannot leave `prd.json` half-written."""
    seen: list[Path] = []
    edit_artifact(PRD_A, editor=lambda p: seen.append(p), write=lambda t: None, ask=lambda p: "n")
    assert seen and seen[0].name == "prd.json"
    assert not seen[0].exists()  # the temporary directory is gone with it


def test_what_the_editor_opens_is_what_is_on_disk() -> None:
    """A prettier render would mean editing something that is not the artifact."""
    seen: list[str] = []

    def editor(path: Path) -> None:
        seen.append(path.read_text())

    edit_artifact(PRD_A, editor=editor, write=lambda t: None, ask=lambda p: "n")
    assert json.loads(seen[0]) == PRD_A.model_dump()


def test_render_is_the_artifact_and_not_a_summary() -> None:
    """A gate that hides a field approves it."""

    class Odd(BaseModel):
        kept: str = "yes"

    assert json.loads(render(Odd())) == {"kept": "yes"}


def test_a_gate_decision_is_a_plain_value() -> None:
    with pytest.raises(ValidationError):
        Decision(action="maybe")  # type: ignore[arg-type]
