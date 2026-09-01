"""WP-8.3 — theme as data, capability degradation, the spinner and the effort gradient.

FR-ANIM-01 (the working indicator and its 15 fps cap), FR-ANIM-03 (the gradient and its drops),
FR-ANIM-04 (nothing moves and no escape byte survives without a colour TTY), FR-ANIM-05 (a theme
file swaps assets with no code edit), FR-ANIM-06 (the terminal is restored on any exit).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from loom.tui.anim import (
    animating,
    detect_capability,
    gradient_track,
    spinner_frame,
    sweep_phase,
)
from loom.tui.live import SHOW_AFTER_S, WorkingIndicator, working_line
from loom.tui.theme import DEFAULT_THEME, Theme, ThemeError


class Clock:
    """A hand-cranked monotonic clock so timing is asserted exactly, never slept for."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _write_theme(root: Path, body: str) -> None:
    (root / ".loom").mkdir(parents=True, exist_ok=True)
    (root / ".loom" / "theme.toml").write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------- FR-ANIM-05


def test_load_returns_default_without_a_file(tmp_path: Path) -> None:
    assert Theme.load(tmp_path) is DEFAULT_THEME


def test_theme_file_swaps_spinner_frames_with_no_code_edit(tmp_path: Path) -> None:
    # The exact FR-ANIM-05 verification: drop a file, get different frames.
    _write_theme(tmp_path, '[spinner]\nframes = ["◐", "◓", "◑", "◒"]\n')
    theme = Theme.load(tmp_path)
    assert theme.spinner.frames == ("◐", "◓", "◑", "◒")
    # everything the file did not name is still the default
    assert theme.color == DEFAULT_THEME.color
    assert theme.glyph["ok"] == DEFAULT_THEME.glyph["ok"]


def test_load_merges_per_key_and_coerces_sweep_scalar(tmp_path: Path) -> None:
    _write_theme(
        tmp_path,
        '[color]\naccent = "#ffffff"\n[gradient]\nsweep_ms = 3000\n'
        '[glyph]\ntrack = ["<", ">"]\n[logo]\nmark = ["a", "b"]\nglyph = "*"\n',
    )
    theme = Theme.load(tmp_path)
    assert theme.color["accent"] == "#ffffff"
    assert theme.color["ground"] == DEFAULT_THEME.color["ground"]  # untouched key survives
    assert theme.gradient["sweep_ms"] == [3000]  # scalar wrapped to the in-code sequence shape
    assert theme.glyph["track_left"] == "<" and theme.glyph["track_right"] == ">"
    assert list(theme.logo) == ["a", "b"] and theme.glyph["logo"] == "*"


def test_load_refuses_an_unknown_table(tmp_path: Path) -> None:
    _write_theme(tmp_path, '[palette]\naccent = "#fff"\n')
    with pytest.raises(ThemeError, match="unknown theme table"):
        Theme.load(tmp_path)


# --------------------------------------------------------------------------- degrade


def test_degrade_to_ascii_swaps_twins_and_spinner() -> None:
    ascii_theme = DEFAULT_THEME.degrade("ascii")
    assert ascii_theme.glyph["ok"] == "[ok]"  # twin substituted
    assert ascii_theme.glyph["validate"] == DEFAULT_THEME.glyph["validate"]  # no twin, unchanged
    assert ascii_theme.spinner.frames == DEFAULT_THEME.spinner.ascii


def test_degrade_keeps_unicode_above_ascii() -> None:
    assert DEFAULT_THEME.degrade("no_color").glyph["ok"] == "✓"
    assert DEFAULT_THEME.degrade("truecolor") is DEFAULT_THEME


# --------------------------------------------------------------------------- capability ladder


@pytest.mark.parametrize(
    "env, is_tty, expected",
    [
        ({"COLORTERM": "truecolor"}, True, "truecolor"),
        ({"TERM": "xterm-256color"}, True, "ansi256"),
        ({"NO_COLOR": ""}, True, "no_color"),  # presence, not value
        ({"COLORTERM": "truecolor", "NO_COLOR": "1"}, True, "no_color"),  # NO_COLOR wins
        ({"TERM": "dumb"}, True, "ascii"),
        ({"COLORTERM": "truecolor"}, False, "ascii"),  # a pipe is ascii whatever the env says
    ],
)
def test_capability_ladder(env: dict[str, str], is_tty: bool, expected: str) -> None:
    assert detect_capability(env, is_tty=is_tty) == expected


def test_animating_is_off_without_colour_or_with_flag() -> None:
    assert animating("truecolor") and animating("ansi256")
    assert not animating("no_color") and not animating("ascii")
    assert not animating("truecolor", no_animation=True)


# --------------------------------------------------------------------------- spinner + gradient


def test_spinner_advances_and_wraps() -> None:
    frames = DEFAULT_THEME.spinner.frames
    step = DEFAULT_THEME.spinner.interval_ms / 1000
    assert spinner_frame(DEFAULT_THEME, elapsed=0.0) == frames[0]
    assert spinner_frame(DEFAULT_THEME, elapsed=step) == frames[1]
    assert spinner_frame(DEFAULT_THEME, elapsed=step * len(frames)) == frames[0]  # wraps
    assert spinner_frame(DEFAULT_THEME, elapsed=0.0, cap="ascii") == DEFAULT_THEME.spinner.ascii[0]


def test_gradient_degrades_across_capabilities() -> None:
    true = gradient_track(DEFAULT_THEME, 10, "truecolor")
    assert "\x1b[38;2;" in true and true.endswith("\x1b[0m")  # animated 24-bit + reset
    c256 = gradient_track(DEFAULT_THEME, 10, "ansi256")
    assert "\x1b[38;5;" in c256 and "38;2;" not in c256  # static 5-stop, not 24-bit
    for quiet in ("no_color", "ascii"):
        bar = gradient_track(DEFAULT_THEME, 10, quiet)  # type: ignore[arg-type]
        assert "\x1b" not in bar  # FR-ANIM-04 — no escape byte at the quiet rungs


def test_gradient_animation_phase_shifts_the_colours() -> None:
    a = gradient_track(DEFAULT_THEME, 12, "truecolor", phase=0.0)
    b = gradient_track(DEFAULT_THEME, 12, "truecolor", phase=0.5)
    assert a != b  # the sweep actually moves
    assert 0.0 <= sweep_phase(DEFAULT_THEME, 2.75) < 1.0


# --------------------------------------------------------------------------- the indicator


def test_working_line_carries_activity_elapsed_and_cost() -> None:
    line = working_line(
        DEFAULT_THEME, activity="writing app/main.py", elapsed=3.2, usd=0.0142, cap="truecolor"
    )
    assert "writing app/main.py" in line and "3.2s" in line and "$0.0142" in line
    assert "\x1b" not in line  # the text itself carries no escape; the controller adds the repaint


def test_indicator_suppresses_under_the_threshold_then_draws() -> None:
    clock = Clock()
    out = io.StringIO()
    with WorkingIndicator(out, DEFAULT_THEME, cap="truecolor", clock=clock) as ind:
        ind.start("validating")
        clock.t = SHOW_AFTER_S - 0.1
        ind.tick(usd=0.0)
        assert "validating" not in out.getvalue()  # too soon — nothing drawn
        clock.t = SHOW_AFTER_S + 0.1
        ind.tick(usd=0.01)
    drawn = out.getvalue()
    assert "validating" in drawn and "$0.0100" in drawn
    assert drawn.endswith("\x1b[?25h")  # cursor shown again on exit (FR-ANIM-06)


def test_indicator_repaint_is_capped_at_max_fps() -> None:
    clock = Clock()
    out = io.StringIO()
    theme = DEFAULT_THEME  # max_fps 15 -> ~0.0667 s between paints
    with WorkingIndicator(out, theme, cap="truecolor", clock=clock) as ind:
        ind.start("building")
        clock.t = 1.0
        ind.tick(usd=0.0)  # first paint
        clock.t = 1.01
        ind.tick(usd=0.0)  # inside the fps window -> suppressed
        clock.t = 1.20
        ind.tick(usd=0.0)  # past it -> second paint
    assert out.getvalue().count("building") == 2


def test_indicator_is_inert_without_a_colour_tty() -> None:
    # FR-ANIM-04 — on a pipe / dumb terminal the indicator draws nothing (ui.TurnLine renders),
    # and emits no escape byte at all.
    clock = Clock()
    out = io.StringIO()
    with WorkingIndicator(out, DEFAULT_THEME, cap="ascii", clock=clock) as ind:
        ind.start("building")
        clock.t = 5.0
        ind.tick(usd=1.23)
    assert out.getvalue() == ""


def test_indicator_restores_the_terminal_on_a_crash() -> None:
    # FR-ANIM-06 — an exception inside the widget still shows the cursor on the way out.
    clock = Clock()
    out = io.StringIO()
    with pytest.raises(RuntimeError):
        with WorkingIndicator(out, DEFAULT_THEME, cap="truecolor", clock=clock) as ind:
            ind.start("building")
            clock.t = 1.0
            ind.tick(usd=0.0)
            raise RuntimeError("boom")
    assert out.getvalue().endswith("\x1b[?25h")
