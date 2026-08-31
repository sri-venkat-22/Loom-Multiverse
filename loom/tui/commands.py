"""The one command registry. FR-SLASH-05 — every command is declared here once, and the palette,
`/help`, filtering, unknown-command handling and while-running safety all read from this list and
no other. Add a command by adding a `Command`; there is no second place to update.

A command carries its metadata (name, category, one-line description, release gate, optional
keybinding, the widget it opens) and a handler. The handler is thin on purpose: it decides *what*
happens and calls the injected `ReplContext` to do the terminal part, so the decision is unit-
testable with a scripted context and no TTY (SRS §2.5).
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from loom.config import Config
from loom.pipeline import PHASES
from loom.tui.widgets import (
    EFFORT_ORDER,
    ModelChoice,
    ModelOption,
    PhasePlan,
    PhaseRow,
    effort_meaning,
    missing_upstream,
    model_options,
    model_summary,
    phase_rows,
    phase_summary,
    run_rows,
)

#: The four run states free text and while-running gating branch on (FR-REPL-03, FR-SLASH-09).
RunState = Literal["idle", "gate", "running", "finished"]

#: How a command behaves when a phase is already running (FR-SLASH-09). "immediate" applies now,
#: "queue" applies from the next phase, "refuse" is declined with a reason. Nothing corrupts a run.
WhileRunning = Literal["immediate", "queue", "refuse"]

#: Palette group order, §3.3.
CATEGORY_ORDER: tuple[str, ...] = (
    "pipeline",
    "run control",
    "settings",
    "workspace",
    "session",
    "diagnostics",
    "advanced",
)


class ReplContext(Protocol):
    """What a handler needs from the running session. The concrete one lives in `repl.py`; a test
    supplies a stub. Business actions (persisting config, starting phases) live behind these
    methods so the handler itself holds no terminal or pipeline logic."""

    root: Path
    run_id: str
    config: Config

    @property
    def state(self) -> RunState: ...

    def write(self, text: str) -> None: ...

    # interactive parts — the thin terminal shell (widgets.py), injected so tests script them
    def run_model_picker(self, options: list[ModelOption]) -> ModelChoice | None: ...
    def run_slider(self, levels: list[str], meaning: dict[str, str], start: int) -> str | None: ...
    def run_list(
        self, title: str, description: str, rows: list[str], footer: str
    ) -> int | None: ...
    def confirm(self, prompt: str, *, default: bool = True) -> bool: ...

    # business actions — implemented against core, never here
    def persist_default_model(self, model: str) -> None: ...
    def use_model_this_session(self, model: str) -> None: ...
    def persist_default_effort(self, level: str) -> None: ...
    def start_run(self, start: str, stop: str, run_first: tuple[str, ...] = ()) -> None: ...
    def resume(self, run_id: str) -> None: ...
    def clear_session(self) -> None: ...
    def cost_report(self) -> str: ...
    def status_report(self) -> str: ...


Handler = Callable[["ReplContext", str], None]


@dataclass(frozen=True)
class Command:
    name: str
    category: str
    summary: str
    release: str = "R1"
    key: str | None = None
    widget: str | None = None
    while_running: WhileRunning = "immediate"
    handler: Handler | None = field(default=None)


# --------------------------------------------------------------------------- handlers


def _match_model(text: str, options: list[ModelOption]) -> str | None:
    """A typed model argument (FR-SLASH-06) against the catalog — full string or short name."""
    for option in options:
        if text in (option.model, option.short):
            return option.model
    return None


def _model(ctx: ReplContext, args: str) -> None:
    options = model_options(ctx.config)
    if args.strip():  # typed argument skips the widget (FR-SLASH-06); a typed pick is the default
        model = _match_model(args.strip(), options)
        if model is None:
            ctx.write(f"no model {args.strip()!r} — /model with no argument lists them")
            return
        ctx.persist_default_model(model)
        ctx.write(model_summary(ModelChoice(model, "default"), options))
        return
    choice = ctx.run_model_picker(options)
    if choice is None:
        ctx.write(model_summary(None, options))
        return
    if choice.scope == "default":
        ctx.persist_default_model(choice.model)
    else:
        ctx.use_model_this_session(choice.model)
    ctx.write(model_summary(choice, options))


def _effort(ctx: ReplContext, args: str) -> None:
    levels = list(EFFORT_ORDER)
    meaning = {lvl: effort_meaning(lvl) for lvl in levels}
    if args.strip():
        level = args.strip().lower()
        if level not in meaning:
            ctx.write(f"no effort level {level!r} — one of {', '.join(levels)}")
            return
        ctx.persist_default_effort(level)
        ctx.write(f"/effort → {level}")
        return
    start = levels.index(ctx.config.effort) if ctx.config.effort in levels else 1
    chosen = ctx.run_slider(levels, meaning, start)
    if chosen is None:
        ctx.write("/effort → cancelled")
        return
    ctx.persist_default_effort(chosen)
    ctx.write(f"/effort → {chosen}")


def _phase(ctx: ReplContext, args: str) -> None:
    rows = phase_rows(ctx.root, ctx.run_id)
    by_name = {r.phase: r for r in rows}
    if args.strip():
        target = args.strip().lower()
        if target not in by_name:
            ctx.write(f"no phase {target!r} — one of {', '.join(PHASES)}")
            return
        _start_phase(ctx, target, rows)
        return
    display = [
        f"{r.phase:<9} {('✓ done ' + r.produced_ago if r.exists else '—'):<18} {r.effect}".rstrip()
        for r in rows
    ]
    index = ctx.run_list("Start from which phase?", "", display, "Enter to start · Esc to cancel")
    if index is None:
        ctx.write(phase_summary(None))
        return
    _start_phase(ctx, PHASES[index], rows)


def _start_phase(ctx: ReplContext, target: str, rows: list[PhaseRow]) -> None:
    """FR-SLASH-08 — a missing upstream artifact offers to run the missing phases first rather
    than failing."""
    missing = missing_upstream(target, rows)
    if missing:
        if not ctx.confirm(f"{target} needs {', '.join(missing)} first — run them?", default=True):
            ctx.write(phase_summary(None))
            return
        plan = PhasePlan(start=missing[0], run_first=tuple(missing))
        ctx.write(phase_summary(plan))
        ctx.start_run(missing[0], "build", tuple(missing))
        return
    plan = PhasePlan(start=target, run_first=())
    ctx.write(phase_summary(plan))
    ctx.start_run(target, "build", ())


def _resume(ctx: ReplContext, args: str) -> None:
    rows = run_rows(ctx.root)
    if not rows:
        ctx.write("no previous runs to resume")
        return
    if args.strip():
        run_id = args.strip()
        if run_id not in {r.run_id for r in rows}:
            ctx.write(f"no run {run_id!r}")
            return
        ctx.resume(run_id)
        return
    index = ctx.run_list(
        "Resume which run?", "", [r.label for r in rows], "Enter to resume · Esc to cancel"
    )
    if index is None:
        ctx.write("/resume → cancelled")
        return
    ctx.resume(rows[index].run_id)


def _start(start: str, stop: str) -> Handler:
    def handler(ctx: ReplContext, args: str) -> None:
        ctx.start_run(start, stop)

    return handler


def _help(ctx: ReplContext, args: str) -> None:
    ctx.write(render_help())


def _clear(ctx: ReplContext, args: str) -> None:
    ctx.clear_session()
    ctx.write("/clear → new session; the current run stays on disk")


def _cost(ctx: ReplContext, args: str) -> None:
    ctx.write(ctx.cost_report())


def _status(ctx: ReplContext, args: str) -> None:
    ctx.write(ctx.status_report())


def _unimplemented(ctx: ReplContext, args: str) -> None:
    ctx.write("that command is not available in this build yet")


# --------------------------------------------------------------------------- the registry

REGISTRY: list[Command] = [
    # pipeline ──────────────────────────────────────────────────────────────────────
    Command(
        "phase",
        "pipeline",
        "Choose which phase to start from",
        widget="phase",
        while_running="refuse",
        handler=_phase,
    ),
    Command(
        "validate",
        "pipeline",
        "Research the idea: market, competitors, risks → validation.json",
        while_running="refuse",
        handler=_start("validate", "validate"),
    ),
    Command(
        "plan",
        "pipeline",
        "Turn the validation into a PRD → prd.json",
        while_running="refuse",
        handler=_start("plan", "plan"),
    ),
    Command(
        "design",
        "pipeline",
        "Choose the stack, file manifest and rubric → design.json",
        while_running="refuse",
        handler=_start("design", "design"),
    ),
    Command(
        "build",
        "pipeline",
        "Scaffold, write the code, and test until the rubric passes",
        while_running="refuse",
        handler=_start("build", "build"),
    ),
    Command(
        "run",
        "pipeline",
        "Run the whole pipeline end to end",
        while_running="refuse",
        handler=_start("validate", "build"),
    ),
    # run control ───────────────────────────────────────────────────────────────────
    Command(
        "resume",
        "run control",
        "Continue a previous run",
        widget="resume",
        while_running="refuse",
        handler=_resume,
    ),
    Command(
        "replay",
        "run control",
        "Re-run one phase against cached upstream artifacts",
        while_running="refuse",
        handler=_unimplemented,
    ),
    Command(
        "artifacts",
        "run control",
        "Inspect or edit what each phase produced",
        widget="artifacts",
        while_running="refuse",
        handler=_unimplemented,
    ),
    Command(
        "rubric",
        "run control",
        "Show the rubric and the last score breakdown",
        handler=_unimplemented,
    ),
    Command(
        "clear",
        "run control",
        "Start fresh; the current run stays on disk",
        while_running="refuse",
        handler=_clear,
    ),
    # settings ──────────────────────────────────────────────────────────────────────
    Command(
        "model",
        "settings",
        "Switch model, for this session or as the default",
        widget="model",
        while_running="queue",
        handler=_model,
    ),
    Command(
        "effort",
        "settings",
        "Trade speed against thoroughness",
        widget="effort",
        while_running="queue",
        handler=_effort,
    ),
    Command(
        "budget",
        "settings",
        "Set the dollar ceiling for this run",
        while_running="queue",
        handler=_unimplemented,
    ),
    Command("config", "settings", "Edit .loom/config.toml", handler=_unimplemented),
    # workspace ─────────────────────────────────────────────────────────────────────
    Command(
        "add-dir",
        "workspace",
        "Add a directory to the workspace jail",
        while_running="refuse",
        handler=_unimplemented,
    ),
    Command(
        "cd",
        "workspace",
        "Change the working directory",
        while_running="refuse",
        handler=_unimplemented,
    ),
    # session ───────────────────────────────────────────────────────────────────────
    Command("cost", "session", "Spend this session, by phase and by model", handler=_cost),
    Command("status", "session", "Environment and config in effect", handler=_status),
    Command(
        "compact",
        "session",
        "Summarize the conversation to reclaim context",
        while_running="queue",
        handler=_unimplemented,
    ),
    Command("help", "session", "List every command and what it does", handler=_help),
    # diagnostics ───────────────────────────────────────────────────────────────────
    Command(
        "doctor",
        "diagnostics",
        "Check python, git, provider key and network",
        handler=_unimplemented,
    ),
    Command(
        "bug", "diagnostics", "Write a redacted diagnostic bundle to a file", handler=_unimplemented
    ),
    # advanced (R1.1) ───────────────────────────────────────────────────────────────
    Command(
        "background",
        "advanced",
        "Run the current phase in the background",
        release="R1.1",
        while_running="queue",
        handler=_unimplemented,
    ),
    Command(
        "rewind",
        "advanced",
        "Rewind to an earlier turn",
        release="R1.1",
        while_running="refuse",
        handler=_unimplemented,
    ),
]

_BY_NAME: dict[str, Command] = {c.name: c for c in REGISTRY}


# --------------------------------------------------------------------------- lookup + filter


def resolve(name: str) -> Command | None:
    return _BY_NAME.get(name.lstrip("/"))


def suggest(name: str) -> str | None:
    """The closest command name, for the unknown-command hint (FR-SLASH-04)."""
    close = difflib.get_close_matches(name.lstrip("/"), _BY_NAME, n=1, cutoff=0.4)
    return close[0] if close else None


def unknown_message(name: str) -> str:
    hint = suggest(name)
    tail = f" — did you mean `/{hint}`?" if hint else ""
    return f"unknown command `/{name.lstrip('/')}`{tail}"


def _subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(char in it for char in needle)


def filter_commands(query: str) -> list[Command]:
    """FR-SLASH-02 — case-insensitive subsequence over name and description, name matches ranked
    above description matches. An empty query is every command in registry order."""
    q = query.strip().lower()
    if not q:
        return list(REGISTRY)
    name_hits = [c for c in REGISTRY if _subsequence(q, c.name.lower())]
    seen = {id(c) for c in name_hits}
    desc_hits = [
        c
        for c in REGISTRY
        if id(c) not in seen and _subsequence(q, f"{c.name} {c.summary}".lower())
    ]
    return name_hits + desc_hits


# --------------------------------------------------------------------------- rendering


def _grouped(commands: list[Command]) -> list[tuple[str, list[Command]]]:
    by_cat: dict[str, list[Command]] = {}
    for command in commands:
        by_cat.setdefault(command.category, []).append(command)
    ordered = [(cat, by_cat[cat]) for cat in CATEGORY_ORDER if cat in by_cat]
    # a category not in CATEGORY_ORDER should still show, rather than vanish
    ordered += [(cat, cmds) for cat, cmds in by_cat.items() if cat not in CATEGORY_ORDER]
    return ordered


def _rule(category: str, width: int = 74) -> str:
    dashes = "─" * max(3, width - len(category) - 5)
    return f"  ── {category} {dashes}"


def render_palette(query: str = "") -> str:
    """FR-SLASH-01/02 — the filtered palette, grouped by category. No match is a message, never
    an empty box."""
    commands = filter_commands(query)
    if not commands:
        return "  no command matches"
    lines: list[str] = []
    for category, cmds in _grouped(commands):
        lines.append(_rule(category))
        for command in cmds:
            lines.append(f"  /{command.name:<12} {command.summary}")
    return "\n".join(lines)


def render_help() -> str:
    """FR-SLASH-05/07 — every command with its description and keybinding, generated from the same
    registry the palette uses. `/help` is a render of `REGISTRY`, not a second list."""
    lines: list[str] = []
    for category, cmds in _grouped(list(REGISTRY)):
        lines.append(_rule(category))
        for command in cmds:
            key = f"  [{command.key}]" if command.key else ""
            gate = "" if command.release == "R1" else f"  ({command.release})"
            lines.append(f"  /{command.name:<12} {command.summary}{key}{gate}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- dispatch


def dispatch(ctx: ReplContext, line: str) -> None:
    """Run a `/command`. FR-SLASH-04 — an unknown one is a message, never forwarded to the model.
    FR-SLASH-09 — while a phase runs, a command applies, queues with a stated effect, or is
    refused; it never silently corrupts a run."""
    body = line.strip().lstrip("/")
    name, _, args = body.partition(" ")
    command = resolve(name)
    if command is None:
        ctx.write(unknown_message(name))
        return

    if ctx.state == "running":
        if command.while_running == "refuse":
            ctx.write(
                f"/{command.name} can't run while a phase is working — Esc to interrupt first"
            )
            return
        if command.while_running == "queue":
            ctx.write(f"/{command.name} queued — applies from the next phase")
            # fall through so the choice is still made now; only its effect is deferred
    if command.handler is None:
        _unimplemented(ctx, args)
        return
    command.handler(ctx, args)
