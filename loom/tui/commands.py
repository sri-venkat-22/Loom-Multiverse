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
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from loom.config import Config, config_sources
from loom.contracts import Design
from loom.phases.base import artifact_path
from loom.pipeline import PHASES
from loom.rubric import Score
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

#: Phases `/replay` can re-run: the Shape A phases, which produce one JSON artifact from cached
#: upstream. `build` is not replayable — it is the whole agent loop, not a single re-prompt.
REPLAYABLE: tuple[str, ...] = ("validate", "plan", "design")

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
    def persist_config(self, key: str, value: object) -> None: ...
    def start_run(self, start: str, stop: str, run_first: tuple[str, ...] = ()) -> None: ...
    def resume(self, run_id: str) -> None: ...
    def replay(self, phase: str) -> None: ...
    def change_dir(self, path: Path) -> None: ...
    def add_dir(self, path: Path) -> None: ...
    def clear_session(self) -> None: ...
    def cost_report(self) -> str: ...
    def status_report(self) -> str: ...
    def doctor_report(self) -> str: ...
    def write_bug_bundle(self) -> str: ...
    def compact_context(self) -> str: ...


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


def _load_score(root: Path, run_id: str) -> Score | None:
    """The last grading for this run (`score.json`, FR-ART-04), or None if absent or torn."""
    path = artifact_path(root, run_id, "build")
    if not path.is_file():
        return None
    try:
        return Score.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None


def rubric_report(root: Path, run_id: str) -> str:
    """The design's rubric with the last score beside each criterion — the mockup's `/rubric`."""
    design_path = artifact_path(root, run_id, "design")
    if not design_path.is_file():
        return "no rubric yet — run /design to produce one"
    design = Design.model_validate_json(design_path.read_text(encoding="utf-8"))
    rubric = design.rubric
    score = _load_score(root, run_id)
    scored = {c.name: c for c in score.criteria} if score else {}
    lines = [f"Rubric — threshold {rubric.threshold:.2f}"]
    for c in rubric.criteria:
        hard = "  hard-fail" if c.name in rubric.hard_fail else ""
        got = f"  → {scored[c.name].score:.2f}" if c.name in scored else ""
        lines.append(f"  {c.name:<22} {c.kind:<6} w{c.weight:.2f}{hard}{got}")
    if score is not None:
        lines.append(f"last score {score.total:.2f} against threshold {rubric.threshold:.2f}")
    return "\n".join(lines)


def _rubric(ctx: ReplContext, args: str) -> None:
    ctx.write(rubric_report(ctx.root, ctx.run_id))


def _existing_artifacts(root: Path, run_id: str) -> list[tuple[str, Path]]:
    pairs = [(phase, artifact_path(root, run_id, phase)) for phase in PHASES]
    return [(phase, path) for phase, path in pairs if path.is_file()]


def _artifacts(ctx: ReplContext, args: str) -> None:
    """FR-ART-02 — list what the run produced and open one, with the render the gate uses (the
    JSON on disk). A typed phase name skips the picker (FR-SLASH-06)."""
    existing = _existing_artifacts(ctx.root, ctx.run_id)
    if not existing:
        ctx.write("no artifacts yet for this run")
        return
    if args.strip():
        target = args.strip().lower()
        match = next((p for phase, p in existing if target in (phase, p.name)), None)
        if match is None:
            have = ", ".join(phase for phase, _ in existing)
            ctx.write(f"no artifact {target!r} for this run — have: {have}")
            return
        ctx.write(match.read_text(encoding="utf-8"))
        return
    rows = [f"{phase:<9} {path.name}" for phase, path in existing]
    index = ctx.run_list("Which artifact?", "", rows, "Enter to open · Esc to cancel")
    if index is None:
        ctx.write("/artifacts → cancelled")
        return
    ctx.write(existing[index][1].read_text(encoding="utf-8"))


def config_report(root: Path, config: Config) -> str:
    """FR-CFG-04 — every effective setting with the layer it came from."""
    sources = config_sources(cwd=root)
    data = config.model_dump()
    lines = ["setting                value                          source"]
    for key in Config.model_fields:
        if key == "price_table":  # a table, not a scalar; edited in config.toml directly
            continue
        lines.append(f"  {key:<20} {str(data[key]):<30} {sources.get(key, 'default')}")
    return "\n".join(lines)


def _coerce_config(config: Config, key: str, raw: str) -> tuple[Any, str | None]:
    """Validate `key = raw` by round-tripping the whole Config through pydantic, so a bad value or
    an out-of-range effort is refused *before* it is written (never a broken config on disk)."""
    if key not in Config.model_fields:
        return None, f"no such setting {key!r} — /config lists them"
    if key == "price_table":
        return None, "price_table is edited in .loom/config.toml directly"
    try:
        trial = Config(**{**config.model_dump(), key: raw})
    except ValidationError as exc:
        return None, f"invalid {key}: {exc.errors()[0]['msg']}"
    return getattr(trial, key), None


def _config(ctx: ReplContext, args: str) -> None:
    parts = args.split(maxsplit=1)
    if not parts:
        ctx.write(config_report(ctx.root, ctx.config))
        return
    if len(parts) == 1:
        ctx.write(f"usage: /config {parts[0]} <value>  (or /config to list)")
        return
    value, error = _coerce_config(ctx.config, parts[0], parts[1])
    if error:
        ctx.write(error)
        return
    ctx.persist_config(parts[0], value)
    ctx.write(f"/config → {parts[0]} = {value}")


def _budget(ctx: ReplContext, args: str) -> None:
    if not args.strip():
        ctx.write(f"budget ceiling ${ctx.config.budget_usd:.2f} per run")
        return
    value, error = _coerce_config(ctx.config, "budget_usd", args.strip())
    if error:
        ctx.write(error)
        return
    ctx.persist_config("budget_usd", value)
    ctx.write(f"/budget → ${value:.2f} per run")


def _replay(ctx: ReplContext, args: str) -> None:
    """FR-SLASH / `loom replay` from the REPL — re-run one Shape A phase against cached inputs."""
    phase = args.strip().lower()
    if phase not in REPLAYABLE:
        ctx.write(f"/replay <phase> — one of {', '.join(REPLAYABLE)}")
        return
    missing = missing_upstream(phase, phase_rows(ctx.root, ctx.run_id))
    if missing:
        ctx.write(f"can't replay {phase} — {', '.join(missing)} not produced yet")
        return
    ctx.write(f"/replay → re-running {phase} against cached inputs")
    ctx.replay(phase)


def _resolve_dir(root: Path, target: str) -> Path | None:
    raw = Path(target).expanduser()
    path = (raw if raw.is_absolute() else root / raw).resolve()
    return path if path.is_dir() else None


def _cd(ctx: ReplContext, args: str) -> None:
    if not args.strip():
        ctx.write("usage: /cd <directory>")
        return
    path = _resolve_dir(ctx.root, args.strip())
    if path is None:
        ctx.write(f"no such directory: {args.strip()}")
        return
    ctx.change_dir(path)
    ctx.write(f"/cd → {path}")


def _add_dir(ctx: ReplContext, args: str) -> None:
    if not args.strip():
        ctx.write("usage: /add-dir <directory>")
        return
    path = _resolve_dir(ctx.root, args.strip())
    if path is None:
        ctx.write(f"no such directory: {args.strip()}")
        return
    ctx.add_dir(path)
    ctx.write(f"/add-dir → {path}")


def _compact(ctx: ReplContext, args: str) -> None:
    """FR-SESS-06 — compact the context on demand and report the tokens reclaimed. The work is
    core (`context.compact_run`); the handler only routes and prints its one-line report."""
    ctx.write(ctx.compact_context())


def _doctor(ctx: ReplContext, args: str) -> None:
    ctx.write(ctx.doctor_report())


def _bug(ctx: ReplContext, args: str) -> None:
    ctx.write(ctx.write_bug_bundle())


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
        handler=_replay,
    ),
    Command(
        "artifacts",
        "run control",
        "Inspect or edit what each phase produced",
        widget="artifacts",
        while_running="refuse",
        handler=_artifacts,
    ),
    Command(
        "rubric",
        "run control",
        "Show the rubric and the last score breakdown",
        handler=_rubric,
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
        handler=_budget,
    ),
    Command("config", "settings", "Edit .loom/config.toml", handler=_config),
    # workspace ─────────────────────────────────────────────────────────────────────
    Command(
        "add-dir",
        "workspace",
        "Add a directory to the workspace jail",
        while_running="refuse",
        handler=_add_dir,
    ),
    Command(
        "cd",
        "workspace",
        "Change the working directory",
        while_running="refuse",
        handler=_cd,
    ),
    # session ───────────────────────────────────────────────────────────────────────
    Command("cost", "session", "Spend this session, by phase and by model", handler=_cost),
    Command("status", "session", "Environment and config in effect", handler=_status),
    Command(
        "compact",
        "session",
        "Summarize the conversation to reclaim context",
        while_running="queue",
        handler=_compact,
    ),
    Command("help", "session", "List every command and what it does", handler=_help),
    # diagnostics ───────────────────────────────────────────────────────────────────
    Command(
        "doctor",
        "diagnostics",
        "Check python, git, provider key and network",
        handler=_doctor,
    ),
    Command("bug", "diagnostics", "Write a redacted diagnostic bundle to a file", handler=_bug),
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
