"""Inline selection widgets — the radio list, effort slider, confirm, phase and run pickers.

FR-SEL-01/02/03, FR-SLASH-06/08. Two halves, and the split is the point:

* **The decision is a pure function.** `model_options`, `effort_meaning`, `phase_rows`,
  `missing_upstream` and the `*_summary` collapses take data and return data. They are what the
  acceptance tests assert, and they hold no terminal state — the boundary SRS §2.5 draws around
  `loom/tui/`.
* **The rendering is a thin `prompt_toolkit` shell.** `run_picker`/`run_slider`/`run_confirm`
  draw a few lines *inline* (never full-screen, so scrollback survives — FR-SEL-01) and return
  the choice the pure half interprets. They take an injected pipe input/output so the whole
  widget is drivable from a test with no TTY (WP-8.2's "against `prompt_toolkit` pipe input").

No colour, glyph or price string is hard-coded here — glyphs come from the `Theme` (CLAUDE.md
invariant) and prices from `config.price_table` (FR-COST-02's table, one source).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import AnyFormattedText
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import Output

from loom.config import EFFORT_PRESETS, MODEL_TIERS, Config, Price
from loom.phases.base import ARTIFACT_FILES, artifact_path
from loom.pipeline import PHASES
from loom.session import list_runs
from loom.tui.theme import DEFAULT_THEME, Theme

# --------------------------------------------------------------------------- model picker


#: Presentation blurbs for the models Loom names itself — "when to use this" (FR-SEL-02). Keyed
#: by the full provider string. A model with no blurb still lists, with its price alone; this is
#: copy, not configuration, so it lives with the widget rather than in `config.py`.
MODEL_BLURBS: dict[str, str] = {
    "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free": "free, tool-capable, 1M context — default",
    "openrouter/nvidia/nemotron-3-ultra-550b-a55b": "paid Nemotron — no free-tier limits",
    "openrouter/qwen/qwen3-coder": "cheap, good enough for most builds",
    "openrouter/qwen/qwen3-coder-480b": "larger Qwen — better on multi-file designs",
    "anthropic/claude-sonnet-5": "most capable — use when a build keeps stalling",
}

#: The order the picker lists models in. Curated rather than dict-order so the default and the
#: cheap→strong progression read top to bottom. A configured model not in this list is appended.
_MODEL_ORDER: tuple[str, ...] = (
    MODEL_TIERS["cheap"],
    "openrouter/qwen/qwen3-coder",
    "openrouter/qwen/qwen3-coder-480b",
    MODEL_TIERS["strong"],
)


@dataclass(frozen=True)
class ModelOption:
    """One row of the model picker."""

    model: str
    blurb: str
    price: str
    is_default: bool

    @property
    def short(self) -> str:
        """The name a human reads — no provider prefix, no `:free` tag."""
        return self.model.split("/")[-1].split(":")[0]


@dataclass(frozen=True)
class ModelChoice:
    """What the picker returns. `scope` is why Enter and `s` differ (FR-SEL-02)."""

    model: str
    scope: str  # "default" (persist to config.toml) | "session" (this run only)


def format_price(price: Price | None) -> str:
    """A model's price as the picker shows it. One number when in==out, two when they differ."""
    if price is None:
        return "price unknown"
    if price.input_per_mtok == 0 and price.output_per_mtok == 0:
        return "free"
    if price.input_per_mtok == price.output_per_mtok:
        return f"${price.input_per_mtok:g}/Mtok"
    return f"${price.input_per_mtok:g}/${price.output_per_mtok:g} per Mtok"


def model_options(config: Config) -> list[ModelOption]:
    """The picker's rows, default first and marked. Prices come from `config.price_table`."""
    order = list(_MODEL_ORDER)
    if config.model not in order:
        order.insert(0, config.model)  # a --model the catalog does not know still shows
    return [
        ModelOption(
            model=model,
            blurb=MODEL_BLURBS.get(model, ""),
            price=format_price(config.price_table.get(model)),
            is_default=(model == config.model),
        )
        for model in order
    ]


def model_summary(choice: ModelChoice | None, options: list[ModelOption]) -> str:
    """The one-line collapse the widget leaves in scrollback (FR-SEL-01)."""
    if choice is None:
        return "/model → cancelled"
    short = next((o.short for o in options if o.model == choice.model), choice.model)
    if choice.scope == "default":
        return f"/model → {short} (default)"
    return f"/model → {short} (this session)"


# --------------------------------------------------------------------------- effort slider

#: The five levels, in slider order. The knobs each one sets live in `config.EFFORT_PRESETS`
#: (FR-SEL-03) — this is only their left-to-right arrangement.
EFFORT_ORDER: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


def effort_meaning(level: str) -> str:
    """What the highlighted level means, shown before Enter confirms it (FR-SEL-03)."""
    preset = EFFORT_PRESETS[level]
    model = MODEL_TIERS[preset.tier].split("/")[-1].split(":")[0]
    return (
        f"{level} — {model} · up to {preset.max_turns} turns · "
        f"${preset.max_usd:g} ceiling per phase"
    )


def effort_summary(level: str | None, *, changed: bool) -> str:
    """One-line collapse for the slider."""
    if level is None:
        return "/effort → cancelled"
    return f"/effort → {level}" + ("" if changed else " (unchanged)")


# --------------------------------------------------------------------------- phase picker


@dataclass(frozen=True)
class PhaseRow:
    """One phase in the `/phase` picker (FR-SLASH-08): does its artifact exist, how old, and
    what starting here would cost or invalidate."""

    phase: str
    exists: bool
    produced_ago: str  # "" when the artifact does not exist
    effect: str


@dataclass(frozen=True)
class PhasePlan:
    """The picker's answer: where to start, and any upstream that has to run first."""

    start: str
    run_first: tuple[str, ...]  # missing upstream phases, in order


def _ago(when: datetime, *, now: datetime) -> str:
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


#: What re-running a phase throws away — the downstream artifacts its output feeds. Static
#: because the pipeline order is (SRS §5.3); a phase invalidates everything after it.
def _effect(phase: str, exists: bool, *, downstream_present: list[str]) -> str:
    if not exists:
        upstream = PHASES[PHASES.index(phase) - 1] if PHASES.index(phase) else None
        need = f"needs {ARTIFACT_FILES[upstream]}" if upstream else "the first phase"
        cost = "  ·  est. $0.60–1.20" if phase == "build" else ""
        return f"not started — {need}{cost}"
    replaced = ARTIFACT_FILES[phase]
    if downstream_present:
        invalid = ", ".join(ARTIFACT_FILES[p] for p in downstream_present)
        return f"re-running replaces {replaced} and invalidates {invalid}"
    return f"re-running replaces {replaced}"


def phase_rows(root: Path, run_id: str, *, now: datetime | None = None) -> list[PhaseRow]:
    """A row per phase, read off disk (FR-SLASH-08). Pure but for the artifact `stat`."""
    now = now or datetime.now(UTC)
    present = {p: artifact_path(root, run_id, p).is_file() for p in PHASES}
    rows: list[PhaseRow] = []
    for i, phase in enumerate(PHASES):
        path = artifact_path(root, run_id, phase)
        exists = present[phase]
        ago = ""
        if exists:
            ago = _ago(datetime.fromtimestamp(path.stat().st_mtime, UTC), now=now)
        downstream = [p for p in PHASES[i + 1 :] if present[p]]
        rows.append(
            PhaseRow(phase, exists, ago, _effect(phase, exists, downstream_present=downstream))
        )
    return rows


def missing_upstream(phase: str, rows: list[PhaseRow]) -> list[str]:
    """Upstream phases whose artifact is missing — what `/phase` offers to run first rather
    than failing (FR-SLASH-08)."""
    by_name = {r.phase: r for r in rows}
    return [p for p in PHASES[: PHASES.index(phase)] if not by_name[p].exists]


def phase_summary(plan: PhasePlan | None) -> str:
    if plan is None:
        return "/phase → cancelled"
    if plan.run_first:
        return f"/phase → run {', '.join(plan.run_first)} then start at {plan.start}"
    return f"/phase → start at {plan.start}"


# --------------------------------------------------------------------------- run picker


@dataclass(frozen=True)
class RunRow:
    run_id: str
    label: str


def run_rows(root: Path) -> list[RunRow]:
    """Previous runs, newest first, for `/resume` (FR-SLASH-06). The label is just the id; the
    spend and gate state are WP-8.3's live line, not this picker's job."""
    return [RunRow(run_id=r, label=r) for r in reversed(list_runs(root))]


# --------------------------------------------------------------------------- the PT shells


def _line(text: str, style: str = "") -> Window:
    return Window(FormattedTextControl(text), height=1, style=style, dont_extend_height=True)


def _app(
    layout: Layout, bindings: KeyBindings, output: Output | None, input: Input | None
) -> Application[Any]:
    # full_screen=False keeps the widget inline; erasing on exit lets the caller print the
    # one-line summary in its place (FR-SEL-01). A widget must never clear scrollback.
    return Application(
        layout=layout,
        key_bindings=bindings,
        full_screen=False,
        erase_when_done=True,
        output=output,
        input=input,
    )


def run_picker(
    title: str,
    description: str,
    rows: list[str],
    *,
    theme: Theme = DEFAULT_THEME,
    accept: dict[str, str],
    footer: str,
    start: int = 0,
    input: Input | None = None,
    output: Output | None = None,
) -> tuple[int, str] | None:
    """A generic inline list. `accept` maps a key ("enter", or a single char) to the action name
    returned with the selected index. Esc returns None. Powers the model, phase and run pickers.
    """
    state = {"i": start}
    marker = theme.glyph["selected"]

    def render() -> AnyFormattedText:
        lines = []
        for idx, row in enumerate(rows):
            cursor = marker if idx == state["i"] else " " * len(marker)
            lines.append(f"  {cursor} {row}")
        return "\n".join(lines)

    kb = KeyBindings()

    @kb.add("up")
    def _up(event: KeyPressEvent) -> None:
        state["i"] = (state["i"] - 1) % len(rows)

    @kb.add("down")
    def _down(event: KeyPressEvent) -> None:
        state["i"] = (state["i"] + 1) % len(rows)

    @kb.add("escape", eager=True)
    def _cancel(event: KeyPressEvent) -> None:
        event.app.exit(result=None)

    for key, action in accept.items():
        name = "enter" if key == "enter" else key

        def _accept(event: KeyPressEvent, action: str = action) -> None:
            event.app.exit(result=(state["i"], action))

        kb.add(name)(_accept)

    body = HSplit(
        [
            _line(f"  {title}"),
            _line(f"  {description}") if description else _line(""),
            _line(""),
            Window(FormattedTextControl(render), dont_extend_height=True),
            _line(""),
            _line(f"  {footer}"),
        ]
    )
    return cast("tuple[int, str] | None", _app(Layout(body), kb, output, input).run())


def run_model_picker(
    options: list[ModelOption],
    *,
    theme: Theme = DEFAULT_THEME,
    input: Input | None = None,
    output: Output | None = None,
) -> ModelChoice | None:
    """FR-SEL-02 — Enter sets the default, `s` uses it this session only, Esc cancels."""
    ok = theme.glyph["ok"]
    rows = [
        f"{o.short:<28} {o.blurb} · {o.price}" + (f"  {ok}" if o.is_default else "")
        for o in options
    ]
    start = next((i for i, o in enumerate(options) if o.is_default), 0)
    picked = run_picker(
        "Select model",
        "Enter sets your default for new sessions; s uses it this session only.",
        rows,
        theme=theme,
        accept={"enter": "default", "s": "session"},
        footer="Enter to set as default · s to use this session only · Esc to cancel",
        start=start,
        input=input,
        output=output,
    )
    if picked is None:
        return None
    index, scope = picked
    return ModelChoice(options[index].model, scope)


def run_slider(
    levels: list[str],
    meaning: dict[str, str],
    *,
    title: str = "Effort",
    start: int = 0,
    theme: Theme = DEFAULT_THEME,
    input: Input | None = None,
    output: Output | None = None,
) -> str | None:
    """FR-SEL-03 — ←/→ move, Enter confirms, Esc cancels, and the highlighted level's meaning
    shows before you commit to it. The level→config mapping is `config.EFFORT_PRESETS`."""
    state = {"i": start}
    left, right = theme.glyph["track_left"], theme.glyph["track_right"]
    mark = theme.glyph["slider_mark"]

    def track() -> str:
        cells = "   ".join((mark if i == state["i"] else "·") for i in range(len(levels)))
        return f"  Faster {left}  {cells}  {right} Smarter"

    def labels() -> str:
        return "  " + "  ".join(
            (f"[{lvl}]" if i == state["i"] else f" {lvl} ") for i, lvl in enumerate(levels)
        )

    def meaning_line() -> str:
        return f"  {meaning[levels[state['i']]]}"

    kb = KeyBindings()

    @kb.add("left")
    def _left(event: KeyPressEvent) -> None:
        state["i"] = max(0, state["i"] - 1)

    @kb.add("right")
    def _right(event: KeyPressEvent) -> None:
        state["i"] = min(len(levels) - 1, state["i"] + 1)

    @kb.add("enter")
    def _ok(event: KeyPressEvent) -> None:
        event.app.exit(result=levels[state["i"]])

    @kb.add("escape", eager=True)
    def _cancel(event: KeyPressEvent) -> None:
        event.app.exit(result=None)

    body = HSplit(
        [
            _line(f"  {title}"),
            Window(FormattedTextControl(track), dont_extend_height=True),
            Window(FormattedTextControl(labels), dont_extend_height=True),
            _line(""),
            Window(FormattedTextControl(meaning_line), dont_extend_height=True),
            _line("  ←/→ to adjust · Enter to confirm · Esc to cancel"),
        ]
    )
    return cast("str | None", _app(Layout(body), kb, output, input).run())


def run_confirm(
    prompt: str,
    *,
    default: bool = True,
    input: Input | None = None,
    output: Output | None = None,
) -> bool:
    """A yes/no line. Enter takes the default; y/n force it. Used by `/phase`'s run-upstream
    offer and any command that needs a second look."""
    kb = KeyBindings()

    @kb.add("y")
    @kb.add("Y")
    def _yes(event: KeyPressEvent) -> None:
        event.app.exit(result=True)

    @kb.add("n")
    @kb.add("N")
    def _no(event: KeyPressEvent) -> None:
        event.app.exit(result=False)

    @kb.add("enter")
    def _default(event: KeyPressEvent) -> None:
        event.app.exit(result=default)

    @kb.add("escape", eager=True)
    def _cancel(event: KeyPressEvent) -> None:
        event.app.exit(result=False)

    suffix = "[Y/n]" if default else "[y/N]"
    body = HSplit([_line(f"  {prompt} {suffix} ")])
    return bool(_app(Layout(body), kb, output, input).run())
