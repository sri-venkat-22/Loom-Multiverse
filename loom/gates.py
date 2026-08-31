"""Approval gates between phases. WP-3.5 — FR-GATE-01/02/03/04/06, FR-ART-03, FR-VAL-04.

One of the three modules allowed to write to the terminal. Everything it shows is also an
event, so a piped run loses nothing but the prompt.

The gate is the place a founder gets to disagree with the machine before the machine spends
money acting on it. Three actions, and the difference between two of them is the whole design:

* **approve** — go on.
* **reject** — the feedback is appended to the phase *input* and the phase runs again. The
  model does not see its own rejected artifact, only what the reviewer said about it; anchoring
  it to the thing being rejected is how you get the same artifact back with nicer wording.
* **edit** — the human wrote the answer themselves. It is validated and used, and the phase
  does **not** re-run. Paying a model to reproduce a decision a human just made is waste.

`abandon` exists for FR-VAL-04: a `no-go` verdict is a real answer, and the pipeline has to be
stoppable at that gate rather than proceeding to plan an idea the research just rejected.
"""

from __future__ import annotations

import difflib
import json
import os
import shlex
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

Action = Literal["approve", "reject", "edit", "abandon"]

#: What `$EDITOR` falls back to. `vi` is on every POSIX box this runs on (NFR-PORT-01).
DEFAULT_EDITOR = "vi"


class Decision(BaseModel):
    """What the reviewer chose. `artifact` is set only for `edit`."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    action: Action
    feedback: str = ""
    artifact: Any = None

    @property
    def reruns(self) -> bool:
        return self.action == "reject"


class Gate(Protocol):
    """Anything the pipeline can ask for a decision. `--yes` supplies one; so does WP-8.2."""

    def __call__(
        self, phase: str, artifact: BaseModel, previous: BaseModel | None = None
    ) -> Decision: ...


# --------------------------------------------------------------------------- rendering


def render(artifact: BaseModel) -> str:
    """The artifact as a human reads it. JSON, because that is what is on disk and what they
    would be editing — a prettier summary that hides a field is a gate that approves it."""
    return artifact.model_dump_json(indent=2)


def diff(previous: BaseModel | None, current: BaseModel, *, phase: str = "artifact") -> str:
    """FR-GATE-03 — a re-run is reviewed against what it replaced, not in isolation.

    Reviewing the second attempt on its own merits is how a re-run silently drops the one
    section that was right the first time.
    """
    if previous is None:
        return render(current)
    lines = list(
        difflib.unified_diff(
            render(previous).splitlines(keepends=True),
            render(current).splitlines(keepends=True),
            fromfile=f"{phase} (previous)",
            tofile=f"{phase} (new)",
            n=3,
        )
    )
    if not lines:
        return f"({phase} is byte-identical to the previous attempt)"
    return "".join(lines)


# --------------------------------------------------------------------------- the gates


class AutoApprove:
    """FR-GATE-04 — `--yes` and unattended mode. Every auto-approval is still an event, so a
    headless run's history is indistinguishable from a watched one's."""

    def __init__(self, on_event: Callable[..., Any] | None = None) -> None:
        self.on_event = on_event
        self.seen: list[str] = []

    def __call__(
        self, phase: str, artifact: BaseModel, previous: BaseModel | None = None
    ) -> Decision:
        self.seen.append(phase)
        _emit(self.on_event, "gate_opened", phase=phase, interactive=False)
        _emit(self.on_event, "gate_decision", phase=phase, action="approve", auto=True)
        return Decision(action="approve")


class TerminalGate:
    """FR-GATE-01 — the interactive gate. `ask`/`write` are injected so the whole thing is
    testable with a scripted reviewer and no TTY."""

    def __init__(
        self,
        *,
        ask: Callable[[str], str] | None = None,
        write: Callable[[str], None] | None = None,
        editor: Callable[[Path], None] | None = None,
        on_event: Callable[..., Any] | None = None,
    ) -> None:
        self.ask = ask or input
        self.write = write or (lambda text: print(text))  # noqa: T201 - allowed here
        self.editor = editor or open_in_editor
        self.on_event = on_event

    def __call__(
        self, phase: str, artifact: BaseModel, previous: BaseModel | None = None
    ) -> Decision:
        _emit(self.on_event, "gate_opened", phase=phase, interactive=True)
        self.write(f"\n── {phase} ─────────────────────────────────────────")
        self.write(diff(previous, artifact, phase=phase))

        while True:
            choice = (
                self.ask("[a]pprove  [r]eject with feedback  [e]dit  [q]uit run > ").strip().lower()
            )

            if choice in {"a", "approve", ""}:
                return self._decide(phase, Decision(action="approve"))
            if choice in {"q", "quit", "abandon"}:
                return self._decide(phase, Decision(action="abandon"))
            if choice in {"r", "reject"}:
                feedback = self.ask("What is wrong with it? > ").strip()
                if not feedback:
                    self.write(
                        "A rejection with no feedback re-runs the same phase for the "
                        "same answer. Say what to change."
                    )
                    continue
                return self._decide(phase, Decision(action="reject", feedback=feedback))
            if choice in {"e", "edit"}:
                edited = edit_artifact(artifact, editor=self.editor, write=self.write, ask=self.ask)
                if edited is None:
                    continue
                return self._decide(phase, Decision(action="edit", artifact=edited))
            self.write("Not one of the options.")

    def _decide(self, phase: str, decision: Decision) -> Decision:
        _emit(
            self.on_event,
            "gate_decision",
            phase=phase,
            action=decision.action,
            feedback=decision.feedback or None,
        )
        return decision


# --------------------------------------------------------------------------- editing


def open_in_editor(path: Path) -> None:
    """`$EDITOR`, or `vi`. Split with `shlex` so `EDITOR="code -w"` works."""
    command = shlex.split(os.environ.get("EDITOR") or os.environ.get("VISUAL") or DEFAULT_EDITOR)
    subprocess.run([*command, str(path)], check=True)


def edit_artifact(
    artifact: BaseModel,
    *,
    editor: Callable[[Path], None],
    write: Callable[[str], None],
    ask: Callable[[str], str],
) -> BaseModel | None:
    """FR-GATE-06 / FR-ART-03 — edit a copy, re-validate on save, reopen on an invalid edit.

    Returns the edited artifact, or `None` if the reviewer gave up — signalled by saving the
    file unchanged after a failed validation, which is what someone does when they cannot see
    how to fix it. There is no attempt cap: refusing a human's third try at their own file
    would be the tool deciding it knows better.
    """
    model = type(artifact)
    with tempfile.TemporaryDirectory(prefix="loom-edit-") as tmp:
        path = Path(tmp) / f"{model.__name__.lower()}.json"
        path.write_text(render(artifact) + "\n", encoding="utf-8")
        last = path.read_text(encoding="utf-8")

        while True:
            editor(path)
            text = path.read_text(encoding="utf-8")
            try:
                return model.model_validate_json(text)
            except ValidationError as exc:
                if text == last:
                    write("Unchanged — leaving the artifact as it was.")
                    return None
                last = text
                write(f"That is not a valid {model.__name__}:\n{_errors(exc)}")
                if ask("Reopen the editor? [Y/n] > ").strip().lower() in {"n", "no"}:
                    return None
            except json.JSONDecodeError as exc:  # pragma: no cover - pydantic wraps this
                write(f"That is not valid JSON: {exc}")


def _errors(exc: ValidationError) -> str:
    return "\n".join(
        f"  - {'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()
    )


def _emit(on_event: Callable[..., Any] | None, kind: str, **fields: Any) -> None:
    if on_event is not None:
        on_event(kind, **fields)
