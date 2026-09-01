"""WP-8.1 — the session shell and its banner.

FR-CLI-01 (no REPL without a TTY), FR-CLI-02 (banner is a pure function; every field shows),
FR-CLI-03 (the key's source, never the key), FR-CLI-05 (first-run offer), FR-CLI-06 (exit codes).
"""

from __future__ import annotations

import io
from dataclasses import replace

from typer.testing import CliRunner

from loom import cli
from loom.cli import EXIT_FOR_STATUS, ExitCode, app
from loom.tui.app import start_session
from loom.tui.banner import BannerState, render_banner
from loom.tui.theme import DEFAULT_THEME


def _state(**over: object) -> BannerState:
    base: dict[str, object] = dict(
        version="9.9.9",
        model="qwen3-coder",
        context_tokens=262_144,
        effort="medium",
        billing="BYOK",
        provider="openrouter",
        cwd="~/Desktop/my-startup",
        credential_source="OPENROUTER_API_KEY (env)",
        run_state="no run in progress",
        mode="auto",
        theme=DEFAULT_THEME,
    )
    base.update(over)
    return BannerState(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- FR-CLI-02


def test_banner_shows_every_field() -> None:
    out = render_banner(_state())
    for field in (
        "Loom v9.9.9",  # product + version
        "qwen3-coder",  # active model
        "262k context",  # its context window
        "medium effort",  # active effort
        "BYOK",  # billing mode
        "openrouter",  # provider
        "~/Desktop/my-startup",  # working directory
        "OPENROUTER_API_KEY (env)",  # credential source
        "no run in progress",  # run state
        "auto mode on",  # mode
    ):
        assert field in out, field


def test_banner_is_pure() -> None:
    state = _state()
    assert render_banner(state) == render_banner(state)


def test_render_banner_can_drop_the_input_chrome() -> None:
    # The interactive session passes include_input=False so the live REPL owns the prompt, the
    # rule and the mode hint; drawing a second static copy here is what put the cursor above the
    # line you actually typed on. The still (default) keeps the full frame.
    full = render_banner(_state())
    header = render_banner(_state(), include_input=False)
    assert "Loom v9.9.9" in header and "no run in progress" in header  # header unchanged
    assert "auto mode on" in full and "auto mode on" not in header  # hint only in the still
    assert DEFAULT_THEME.glyph["cursor"] in full  # the still keeps the prompt block …
    assert DEFAULT_THEME.glyph["cursor"] not in header  # … the live header does not


def test_banner_colours_on_truecolor_but_keeps_fields_greppable() -> None:
    # FR-ANIM-03 — a truecolor terminal gets 24-bit SGR, and no colour splits a field it wraps.
    out = render_banner(_state(cap="truecolor"))
    assert "\x1b[38;2;" in out
    for field in ("Loom v9.9.9", "qwen3-coder", "262k context", "auto mode on"):
        assert field in out, field


def test_banner_stays_plain_off_the_colour_rungs() -> None:
    # FR-ANIM-04 — NO_COLOR / a pipe / a dumb term drops every escape byte, Unicode kept.
    for cap in ("no_color", "ascii"):
        assert "\x1b[" not in render_banner(_state(cap=cap))


def test_context_window_formats_or_is_omitted() -> None:
    assert "262k context" in render_banner(_state(context_tokens=262_144))
    assert "1M context" in render_banner(_state(context_tokens=1_000_000))
    # Unknown context is omitted, never rendered as "(None)".
    assert "context" not in render_banner(_state(context_tokens=None))


# --------------------------------------------------------------------------- FR-CLI-03


def test_banner_states_the_source_never_the_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret-abcd1234")
    out = render_banner(_state(credential_source="OPENROUTER_API_KEY (env)"))
    assert "sk-secret-abcd1234" not in out
    assert "1234" not in out  # not even the last four


def test_credential_source_names_the_variable_not_its_value(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-topsecret-9999")
    src = cli._credential_source("openrouter/qwen/qwen3-coder", set_from_file=[])
    assert src == "OPENROUTER_API_KEY (env)"
    assert "9999" not in src

    assert cli._credential_source("ollama/llama3", []) == "local model — no key needed"
    assert cli._credential_source("openrouter/x", ["OPENROUTER_API_KEY"]) == (
        "~/.loom/credentials.json"
    )


# ------------------------------------------------------ no hard-coded glyphs (CLAUDE.md invariant)


def test_glyphs_and_logo_come_from_the_theme() -> None:
    assert DEFAULT_THEME.glyph["mode"] in render_banner(_state())  # ⏵⏵
    assert DEFAULT_THEME.logo[0] in render_banner(_state())  # ▀▄▀▄▀
    # Swapping the theme changes the render with no code change — the FR-ANIM-05 property.
    custom = replace(DEFAULT_THEME, logo=("XYZ0", "XYZ1", "XYZ2"))
    assert "XYZ0" in render_banner(_state(theme=custom))


def test_default_theme_carries_the_section_12_glyph_set() -> None:
    # A transcription guard: the phase-progression and status marks the design defines.
    for key in ("validate", "plan", "design", "build", "ok", "fail", "hard_fail", "denied"):
        assert DEFAULT_THEME.glyph[key]
    for key in ("ok", "fail", "hard_fail", "denied"):
        assert DEFAULT_THEME.glyph_ascii[key]  # every mark has an ASCII twin


# --------------------------------------------------------------------------- FR-CLI-01 / 05


def test_no_tty_renders_the_banner_but_never_starts_the_repl() -> None:
    out = io.StringIO()
    started = []
    code = start_session(
        state=_state(),
        initialised=True,
        is_tty=False,
        out=out,
        ask=lambda _p: "y",
        on_init=lambda: None,
        repl=lambda _s: started.append(1) or 7,
    )
    assert code == 0
    assert not started  # FR-CLI-01
    assert "Loom v" in out.getvalue()


def test_tty_runs_the_injected_repl() -> None:
    code = start_session(
        state=_state(),
        initialised=True,
        is_tty=True,
        out=io.StringIO(),
        ask=lambda _p: "",
        on_init=lambda: None,
        repl=lambda _s: 5,
    )
    assert code == 5


def test_interactive_session_omits_the_input_chrome_the_repl_draws_live() -> None:
    # FR-CLI-01 fix — with a live REPL the printed banner is the header only, so the pulsing
    # prompt below it is the one true input line (no static cursor block above it).
    out = io.StringIO()
    start_session(
        state=_state(),  # cap defaults to no_color → printed at once, no stagger
        initialised=True,
        is_tty=True,
        out=out,
        ask=lambda _p: "",
        on_init=lambda: None,
        repl=lambda _s: 0,
    )
    printed = out.getvalue()
    assert "Loom v9.9.9" in printed and "no run in progress" in printed
    assert "auto mode on" not in printed  # the mode hint is the live toolbar's job now
    assert DEFAULT_THEME.glyph["cursor"] not in printed  # no second, static prompt block


def test_non_interactive_still_keeps_the_whole_frame() -> None:
    out = io.StringIO()
    start_session(
        state=_state(),
        initialised=True,
        is_tty=False,  # piped — no live REPL, so the still must be complete
        out=out,
        ask=lambda _p: "",
        on_init=lambda: None,
        repl=lambda _s: 0,
    )
    assert "auto mode on" in out.getvalue()


def test_reveal_writes_every_line_in_order() -> None:
    from loom.tui.app import _reveal

    out = io.StringIO()
    _reveal(["a", "b", "c"], out, delay=0.0)
    assert out.getvalue() == "a\nb\nc\n"


def test_first_run_offer_accepted_initialises_then_shows_the_banner() -> None:
    out = io.StringIO()
    inited = []
    code = start_session(
        state=_state(),
        initialised=False,
        is_tty=True,
        out=out,
        ask=lambda _p: "y",
        on_init=lambda: inited.append(1),
    )
    assert code == 0
    assert inited == [1]
    assert "Loom v" in out.getvalue()


def test_first_run_offer_declined_exits_0_without_initialising() -> None:
    out = io.StringIO()
    inited = []
    code = start_session(
        state=_state(),
        initialised=False,
        is_tty=True,
        out=out,
        ask=lambda _p: "",  # plain Enter means no
        on_init=lambda: inited.append(1),
    )
    assert code == 0  # FR-CLI-05 — declining exits 0
    assert not inited
    assert "Loom v" not in out.getvalue()


def test_no_tty_and_uninitialised_hints_and_exits_0() -> None:
    out = io.StringIO()
    code = start_session(
        state=_state(),
        initialised=False,
        is_tty=False,
        out=out,
        ask=lambda _p: "y",
        on_init=lambda: None,
    )
    assert code == 0
    assert "loom init" in out.getvalue()


def test_bare_loom_help_lists_every_subcommand() -> None:
    out = CliRunner().invoke(app, ["--help"]).output
    for sub in (
        "init",
        "status",
        "build",
        "validate",
        "plan",
        "design",
        "run",
        "resume",
        "replay",
        "cost",
    ):
        assert sub in out


def test_bare_loom_in_a_project_prints_the_banner_and_exits_0(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate credentials + cwd display
    runner = CliRunner()
    assert runner.invoke(app, ["init", "-C", str(tmp_path)]).exit_code == 0
    result = runner.invoke(app, ["-C", str(tmp_path)])  # no subcommand → the session (no TTY)
    assert result.exit_code == 0
    assert "Loom v" in result.output


# --------------------------------------------------------------------------- FR-CLI-06


def test_every_exit_code_0_to_5_has_a_case() -> None:
    # The stable numbers (FR-CLI-06) ...
    assert [int(ExitCode.OK), int(ExitCode.ERROR), int(ExitCode.USAGE)] == [0, 1, 2]
    assert [int(ExitCode.BUDGET), int(ExitCode.RUBRIC), int(ExitCode.INTERRUPTED)] == [3, 4, 5]
    # ... and the pipeline outcome that produces each.
    assert EXIT_FOR_STATUS["passed"] == ExitCode.OK  # 0
    assert EXIT_FOR_STATUS["invalid"] == ExitCode.ERROR  # 1
    assert EXIT_FOR_STATUS["budget_exhausted"] == ExitCode.BUDGET  # 3
    assert EXIT_FOR_STATUS["stalled"] == ExitCode.RUBRIC  # 4


def test_usage_and_success_codes_are_reachable_from_the_cli(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    # 2 — a session cannot start where config is unreadable is hard to force; use a real usage
    # error instead: running a phase in an uninitialised dir.
    usage = runner.invoke(app, ["run", "an idea", "-C", str(tmp_path / "empty"), "--yes"])
    assert usage.exit_code == ExitCode.USAGE
    # 0 — init succeeds.
    assert runner.invoke(app, ["init", "-C", str(tmp_path)]).exit_code == ExitCode.OK
