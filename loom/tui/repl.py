"""The REPL: a persistent prompt, input routed by run state, and the four typed prefixes.

FR-REPL-01..10, FR-KEY-02/04. The split that keeps `loom/tui/` presentation-only (SRS §2.5):

* **Parsing and routing are pure functions.** `classify_input` and `route_free_text` take a
  string and the run state and return what should happen and the one line to say about it
  (FR-REPL-03/04). They are what the tests assert, with no terminal in sight.
* **Side effects go through injected actions or core.** Free text that starts a run calls a
  `ReplActions` callable the CLI wires to the real pipeline; `!` goes through the agent's own
  `bash_tool` (the *same* guard, FR-REPL-09); `#` calls `session.append_note` (FR-REPL-10);
  `/model` persists via `config.set_project_config_value`. The REPL itself owns none of it.

The interactive loop (`Repl.run`) is a `prompt_toolkit` `PromptSession` — history, `@`/`/`
completion, multi-line, and the shift-tab mode cycle. It takes an injected pipe input/output so
the whole thing is drivable from a test with no TTY.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, AnyFormattedText
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.output import Output

from loom import diagnostics
from loom.agent.tools.bash import DEFAULT_TIMEOUT, MAX_OUTPUT_CHARS, _run
from loom.config import Config, load_config, set_project_config_value
from loom.pipeline import PHASES
from loom.security import CommandDenied, check_command
from loom.session import LOOM_DIR, append_note, list_runs
from loom.tui.anim import animating, detect_capability, pulse_prompt
from loom.tui.background import BackgroundRun
from loom.tui.commands import RunState, dispatch
from loom.tui.complete import PathCompleter, SlashCompleter
from loom.tui.theme import DEFAULT_THEME, Theme
from loom.tui.widgets import (
    ModelChoice,
    ModelOption,
    run_confirm,
    run_model_picker,
    run_picker,
    run_slider,
)

# --------------------------------------------------------------------------- parsing + routing


@dataclass(frozen=True)
class Route:
    """Where a line of free text goes, and the one line stated before work begins
    (FR-REPL-03). `kind` is one of noop / new_idea / gate_feedback / queue / followup."""

    kind: str
    line: str
    payload: str = ""


#: The run modes shift-tab cycles through (SRS §3.5, FR-KEY-02).
MODES: tuple[str, ...] = ("auto", "strict", "plan-only", "unattended")


def next_mode(mode: str) -> str:
    """The mode after `mode` in the cycle, wrapping. An unknown mode restarts at the first."""
    try:
        return MODES[(MODES.index(mode) + 1) % len(MODES)]
    except ValueError:
        return MODES[0]


def classify_input(text: str) -> tuple[str, str]:
    """First character decides the lane: `/` command, `!` bash, `#` note, else free text. Empty
    or whitespace-only is its own lane so it can be a no-op (FR-REPL-04)."""
    if not text.strip():
        return "empty", ""
    lead = text.lstrip()
    if lead.startswith("/"):
        return "slash", lead
    if lead.startswith("!"):
        return "bash", lead[1:].strip()
    if lead.startswith("#"):
        return "note", lead[1:].strip()
    return "text", text


def route_free_text(state: RunState, text: str, *, gate_phase: str = "the current") -> Route:
    """FR-REPL-03 — free text means something different in each run state. The returned `line` is
    printed before anything happens, so a misrouted input can be Esc'd out of."""
    if not text.strip():
        return Route("noop", "")
    if state == "idle":
        return Route("new_idea", "→ new run: validating this idea. Esc to cancel.", text)
    if state == "gate":
        return Route(
            "gate_feedback",
            f"→ rejecting the {gate_phase} artifact with this feedback. Esc to cancel.",
            text,
        )
    if state == "running":
        return Route("queue", "→ queued; sent to the model on the next turn. Esc to cancel.", text)
    return Route(
        "followup",
        "→ run finished. Replay design with this note, or start a new run? Esc to cancel.",
        text,
    )


def _background_span(spec: str) -> tuple[str | None, str]:
    """What `/background [phase]` runs. Bare (or `run`) is the whole pipeline; a phase name runs
    that phase alone. An unknown name returns `(None, "")` so the caller can print usage."""
    spec = spec.strip().lower()
    if not spec or spec == "run":
        return PHASES[0], PHASES[-1]
    if spec in PHASES:
        return spec, spec
    return None, ""


def run_bash_line(
    root: Path, command: str, *, env: dict[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT
) -> str:
    """`!` — one shell command through the **same** guard the agent uses (FR-REPL-09). The guard
    is `security.check_command`, called here exactly as `bash_tool`'s `run_bash` calls it, and a
    denial returns its reason rather than running anything."""
    try:
        check_command(command, root=root)  # the identical gate; `!rm -rf /` dies here
    except CommandDenied as exc:
        return str(exc)
    return asyncio.run(
        _run(command, root=root, limit=timeout, env=env, max_output=MAX_OUTPUT_CHARS)
    )


# --------------------------------------------------------------------------- injected actions


def _noop(*_a: object, **_k: object) -> None:
    return None


@dataclass
class ReplActions:
    """The pipeline-touching operations the REPL delegates to. The CLI wires these to real work;
    a test passes stubs. Defaults are no-ops so a REPL is constructible without a pipeline."""

    start_idea: Callable[[str], None] = _noop
    submit_feedback: Callable[[str], None] = _noop
    queue_message: Callable[[str], None] = _noop
    followup: Callable[[str], None] = _noop
    start_run: Callable[[str, str, tuple[str, ...]], None] = _noop
    resume: Callable[[str], None] = _noop
    replay: Callable[[str], None] = _noop
    rewind: Callable[[str], None] = _noop
    run_state: Callable[[], RunState] = lambda: "idle"
    gate_phase: Callable[[], str] = lambda: "the current"
    cost_report: Callable[[], str] = lambda: "no spend recorded"
    status_report: Callable[[], str] = lambda: "no status"


# --------------------------------------------------------------------------- the completer


class _ReplCompleter(Completer):
    """Delegates to the slash palette or path completion by what the line looks like."""

    def __init__(self, root: Path) -> None:
        self._slash = SlashCompleter()
        self._path = PathCompleter(root)

    def get_completions(self, document: Document, complete_event: object) -> Iterable[Completion]:
        text = document.text_before_cursor
        if text.lstrip().startswith("/") and " " not in text.lstrip():
            yield from self._slash.get_completions(document, complete_event)
        elif "@" in text.rsplit(" ", 1)[-1]:
            yield from self._path.get_completions(document, complete_event)


# --------------------------------------------------------------------------- the REPL


class Repl:
    """The interactive session. Implements `ReplContext` (the surface `commands.dispatch` drives)
    and owns the `prompt_toolkit` loop."""

    def __init__(
        self,
        *,
        root: Path,
        config: Config,
        actions: ReplActions | None = None,
        theme: Theme = DEFAULT_THEME,
        out: Callable[[str], None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.config = config
        self.actions = actions or ReplActions()
        self.theme = theme
        self._out = out or (lambda text: print(text))  # noqa: T201 - tui may print
        self.mode: str = config.mode  # widens the Literal so it can cycle (FR-KEY-02)
        runs = list_runs(self.root)
        self.run_id = runs[-1] if runs else ""
        self._extra_dirs: list[Path] = []  # /add-dir, for the session (FR-SEC-03)
        self._widget_input: Input | None = None
        self._widget_output: Output | None = None
        self._bg: BackgroundRun | None = None  # /background (FR-SESS-07)

    # --------------------------------------------------------------- ReplContext surface

    @property
    def state(self) -> RunState:
        # A live background run is the only way the synchronous REPL rests in `running`; it makes
        # `while_running="refuse"` commands refuse, which is what keeps two runs off one workspace.
        if self._bg is not None and self._bg.alive:
            return "running"
        return self.actions.run_state()

    def write(self, text: str) -> None:
        self._out(text)

    def run_model_picker(self, options: list[ModelOption]) -> ModelChoice | None:
        return run_model_picker(
            options, theme=self.theme, input=self._widget_input, output=self._widget_output
        )

    def run_slider(self, levels: list[str], meaning: dict[str, str], start: int) -> str | None:
        return run_slider(
            levels,
            meaning,
            start=start,
            theme=self.theme,
            input=self._widget_input,
            output=self._widget_output,
        )

    def run_list(self, title: str, description: str, rows: list[str], footer: str) -> int | None:
        picked = run_picker(
            title,
            description,
            rows,
            theme=self.theme,
            accept={"enter": "select"},
            footer=footer,
            input=self._widget_input,
            output=self._widget_output,
        )
        return None if picked is None else picked[0]

    def confirm(self, prompt: str, *, default: bool = True) -> bool:
        return run_confirm(
            prompt, default=default, input=self._widget_input, output=self._widget_output
        )

    def persist_default_model(self, model: str) -> None:
        set_project_config_value(self.root, "model", model)
        self.config = self.config.model_copy(update={"model": model})

    def use_model_this_session(self, model: str) -> None:
        self.config = self.config.model_copy(update={"model": model})

    def persist_default_effort(self, level: str) -> None:
        set_project_config_value(self.root, "effort", level)
        # Re-derive the effort-linked knobs from the new preset (drop them so the validator does).
        data = {
            k: v
            for k, v in self.config.model_dump().items()
            if k not in {"max_turns", "max_usd", "rubric_rounds", "model"}
        }
        self.config = Config(**{**data, "effort": level})

    def start_run(self, start: str, stop: str, run_first: tuple[str, ...] = ()) -> None:
        self.actions.start_run(start, stop, run_first)

    def resume(self, run_id: str) -> None:
        self.actions.resume(run_id)

    def persist_config(self, key: str, value: object) -> None:
        set_project_config_value(self.root, key, value)  # type: ignore[arg-type]
        # Reload rather than patch: a written key re-runs the effort-preset derivation and the
        # whole precedence chain, so the in-memory config matches what the next phase will read.
        self.config = load_config(cwd=self.root)

    def replay(self, phase: str) -> None:
        self.actions.replay(phase)

    def background(self, spec: str) -> None:
        """FR-SESS-07 — start the run in a daemon thread and hand the prompt straight back. A run
        already in flight is reported (its events are what `loom status` reads), never doubled."""
        if self._bg is not None and self._bg.alive:
            self.write(f"a {self._bg.label} run is already in the background — /status to watch")
            return
        start, stop = _background_span(spec)
        if start is None:
            self.write(f"/background [phase] — one of {', '.join(PHASES)}, or bare for the run")
            return
        self._bg = BackgroundRun(stop, lambda: self.actions.start_run(start, stop, ()))
        self._bg.start()
        self.write(
            f"/background → {start}→{stop} in the background · /status to watch, /cost for spend"
        )

    def rewind(self, target: str) -> None:
        self.actions.rewind(target)

    def change_dir(self, path: Path) -> None:
        """Re-root the session (FR-SEC-03). Every REPL command reads `self.root`, so `/status`,
        `/cost`, `/artifacts`, `!bash` and the rest follow at once. Starting a *new* pipeline run
        elsewhere is a fresh `loom` in that directory — each project dir owns its own `.loom/`."""
        self.root = path
        runs = list_runs(self.root)
        self.run_id = runs[-1] if runs else ""
        self.config = load_config(cwd=self.root)

    def add_dir(self, path: Path) -> None:
        if path not in self._extra_dirs:
            self._extra_dirs.append(path)

    def clear_session(self) -> None:
        self.run_id = ""

    def cost_report(self) -> str:
        return self.actions.cost_report()

    def status_report(self) -> str:
        return self.actions.status_report()

    def doctor_report(self) -> str:
        checks = diagnostics.doctor(self.root, config=self.config)
        return diagnostics.format_report(
            checks, ok=self.theme.glyph["ok"], fail=self.theme.glyph["fail"]
        )

    def write_bug_bundle(self) -> str:
        path = diagnostics.bug_bundle(self.root, config=self.config)
        return f"wrote a redacted diagnostic bundle → {path}\n(nothing was uploaded)"

    def compact_context(self) -> str:
        from loom.agent.context import compact_run

        return compact_run(self.root, self.run_id)

    # --------------------------------------------------------------- one line of input

    def handle(self, text: str) -> None:
        """Route and act on one submitted line. The pure heart of the loop, so a test can drive
        it directly without a terminal."""
        kind, payload = classify_input(text)
        if kind == "empty":
            return  # FR-REPL-04 — a no-op, never a run
        if kind == "slash":
            dispatch(self, payload)
            return
        if kind == "bash":
            self.write(run_bash_line(self.root, payload))
            return
        if kind == "note":
            path = append_note(self.root, payload)
            if payload.strip():
                self.write(f"# noted → {path.relative_to(self.root)}")
            return
        self._route_text(text)

    def _route_text(self, text: str) -> None:
        route = route_free_text(self.state, text, gate_phase=self.actions.gate_phase())
        if route.kind == "noop":
            return
        self.write(route.line)  # FR-REPL-03 — state the route before working
        {
            "new_idea": self.actions.start_idea,
            "gate_feedback": self.actions.submit_feedback,
            "queue": self.actions.queue_message,
            "followup": self.actions.followup,
        }[route.kind](route.payload)

    # --------------------------------------------------------------- the interactive loop

    def _hint(self) -> str:
        """FR-REPL-02 — mode plus the input prefixes, re-read on every redraw so a mode change
        shows at once."""
        g = self.theme.glyph
        return (
            f"  {g['mode']} {self.mode} mode on (shift+tab to cycle) · "
            "@ for files · ! for bash · # to remember"
        )

    def _history(self) -> History:
        loom = self.root / LOOM_DIR
        return FileHistory(str(loom / "history")) if loom.is_dir() else InMemoryHistory()

    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def _submit(event: KeyPressEvent) -> None:
            buf = event.current_buffer
            if buf.text.rstrip().endswith("\\"):  # FR-REPL-07 — backslash continues the line
                buf.text = buf.text.rstrip()[:-1]
                buf.cursor_position = len(buf.text)
                buf.insert_text("\n")
            else:
                buf.validate_and_handle()

        @kb.add("escape", "enter")  # Alt/Option+Enter — an explicit newline (FR-REPL-07)
        def _newline(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")

        @kb.add("s-tab")  # FR-KEY-02 — cycle the run mode
        def _cycle(event: KeyPressEvent) -> None:
            self.mode = next_mode(self.mode)
            event.app.invalidate()

        return kb

    def run(
        self, state: object = None, *, input: Input | None = None, output: Output | None = None
    ) -> int:
        """The loop. Enter submits, the prompt returns after each line (FR-REPL-01), Ctrl+D exits
        0, and two Ctrl+C within two seconds exit 5 (FR-KEY-04). `state` is the banner state
        `start_session` hands in; the loop does not need its fields, only its presence."""
        self._widget_input, self._widget_output = input, output
        # FR-ANIM-03/04 — the resting prompt pulses through the effort gradient on an animating
        # TTY, and is a plain glyph when piped or on a dumb terminal. Injected I/O (a test) is
        # never a TTY, so the animation and its repaint clock stay off there.
        cap = detect_capability(os.environ, is_tty=input is None and sys.stdout.isatty())
        message: AnyFormattedText
        refresh = 0.0  # prompt_toolkit reads a falsy interval as "no auto-repaint"
        if animating(cap):
            start = time.monotonic()
            message = lambda: ANSI(  # noqa: E731 - a redraw callback, not a stored function
                pulse_prompt(self.theme, elapsed=time.monotonic() - start, cap=cap) + " "
            )
            refresh = 1.0 / max(1, self.theme.max_fps)
        else:
            message = f"{self.theme.glyph['prompt']} "
        session: PromptSession[str] = PromptSession(
            message=message,
            completer=_ReplCompleter(self.root),
            history=self._history(),
            key_bindings=self._bindings(),
            bottom_toolbar=self._hint,
            complete_while_typing=True,
            refresh_interval=refresh,
            input=input,
            output=output,
        )
        last_interrupt = 0.0
        while True:
            try:
                line = session.prompt()
            except EOFError:  # Ctrl+D
                return 0
            except KeyboardInterrupt:  # Ctrl+C
                now = time.monotonic()
                if now - last_interrupt < 2.0:
                    return 5
                last_interrupt = now
                self.write("(interrupted — Ctrl+C again to exit)")
                continue
            self.handle(line)


def make_repl(
    *, root: Path, config: Config, actions: ReplActions | None = None, theme: Theme = DEFAULT_THEME
) -> Callable[[object], int]:
    """A `repl` callable for `start_session` (FR-CLI-01 wiring). Closes over the session state so
    the callback matches `start_session`'s `(state) -> int` shape."""
    return Repl(root=root, config=config, actions=actions, theme=theme).run
