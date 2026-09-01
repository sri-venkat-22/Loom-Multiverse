"""Frame math for the live view — capability detection, spinner frames, the effort gradient.

Pure string generation, no terminal library: every function takes the theme plus the environment
and returns text — ANSI when the terminal can show it, plain when it cannot. The degradation
ladder is §12's, loudest to quietest:

* **truecolor** — the animated 24-bit gradient the SRS singles out (FR-ANIM-03);
* **ansi256** — a static 5-stop 256-colour bar;
* **no_color** — Unicode kept, every colour dropped (`NO_COLOR`);
* **ascii** — the ascii glyph kit, no colour (`TERM=dumb`, a pipe, `--no-animation` on a dumb term).

`--no-animation`, `NO_COLOR`, `TERM=dumb` or a non-TTY stdout each collapse motion to nothing
(`animating`) without changing a single event — only the rendering (FR-ANIM-04).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from loom.tui.theme import Theme

Capability = Literal["truecolor", "ansi256", "no_color", "ascii"]

RESET = "\x1b[0m"


def detect_capability(env: Mapping[str, str], *, is_tty: bool) -> Capability:
    """Where on the ladder this terminal sits. Conservative: truecolor only when `COLORTERM`
    says so, never guessed from `TERM`, so a terminal that cannot render 24-bit never gets it."""
    if not is_tty or (env.get("TERM") or "").lower() == "dumb":
        return "ascii"
    if env.get("NO_COLOR") is not None:  # the spec: presence, not value (an empty NO_COLOR counts)
        return "no_color"
    if (env.get("COLORTERM") or "").lower() in {"truecolor", "24bit"}:
        return "truecolor"
    return "ansi256"


def animating(cap: Capability, *, no_animation: bool = False) -> bool:
    """Whether anything should move. `--no-animation` stops motion at any capability; the lower
    two rungs never moved. `cap` already folds in NO_COLOR / dumb / non-TTY via `detect_capability`.
    """
    return cap in ("truecolor", "ansi256") and not no_animation


def spinner_frame(theme: Theme, *, elapsed: float, cap: Capability = "truecolor") -> str:
    """The spinner glyph for this instant, chosen by elapsed time and the theme's interval. The
    ascii kit on a dumb terminal, the Unicode ramp otherwise."""
    frames = theme.spinner.ascii if cap == "ascii" else theme.spinner.frames
    if not frames:
        return ""
    step = max(1, theme.spinner.interval_ms)
    return frames[int(elapsed * 1000 / step) % len(frames)]


# --------------------------------------------------------------------------- the effort gradient


def _hex_rgb(value: str) -> tuple[int, int, int]:
    h = value.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def _sample(stops: list[tuple[int, int, int]], t: float) -> tuple[int, int, int]:
    """Colour at position `t` along equally-spaced `stops`. `t` wraps, so the symmetric effort
    palette sweeps seamlessly when animated."""
    t %= 1.0
    n = len(stops) - 1
    if n <= 0:
        return stops[0]
    pos = t * n
    i = min(int(pos), n - 1)
    return _lerp(stops[i], stops[i + 1], pos - i)


def sweep_phase(theme: Theme, elapsed: float) -> float:
    """How far through one gradient sweep `elapsed` is, in [0, 1). `sweep_ms` is §12 data."""
    sweep = theme.gradient.get("sweep_ms") or [5500]
    ms = float(sweep[0]) if isinstance(sweep, (list, tuple)) else float(sweep)  # type: ignore[arg-type]
    return (elapsed * 1000.0 / ms) % 1.0 if ms > 0 else 0.0


def gradient_track(theme: Theme, width: int, cap: Capability, *, phase: float = 0.0) -> str:
    """The effort track, `width` cells wide, rendered for `cap`. Truecolor animates with `phase`;
    256-colour is the static 5-stop bar; the quiet rungs are the bare glyph, no escape byte."""
    width = max(1, width)
    weft = theme.glyph.get("weft", "-")
    if cap == "ascii":
        return "-" * width
    if cap == "no_color":
        return weft * width
    if cap == "ansi256":
        codes = list(theme.gradient.get("effort_256") or [])
        if not codes:
            return weft * width
        cells = [f"\x1b[38;5;{codes[i * len(codes) // width]}m{weft}" for i in range(width)]
        return "".join(cells) + RESET
    stops = [_hex_rgb(str(h)) for h in (theme.gradient.get("effort") or [])]
    if not stops:
        return weft * width
    span = max(1, width - 1)
    cells = []
    for i in range(width):
        r, g, b = _sample(stops, i / span + phase)
        cells.append(f"\x1b[38;2;{r};{g};{b}m{weft}")
    return "".join(cells) + RESET
