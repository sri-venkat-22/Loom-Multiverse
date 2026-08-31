"""The theme, as data — the drawing's §12 turned into the shape `FR-ANIM-05` requires.

Every colour, glyph, spinner frame, logo line and gradient stop lives here and nowhere else, so
a widget never hard-codes one (CLAUDE.md invariant). `.loom/theme.toml` overriding these defaults
(`load`) and the truecolor→ascii degradation ladder (`degrade`) are `FR-ANIM-05`/`FR-ANIM-04`,
which land with the live output in WP-8.3 — WP-8.1 needs only the defaults the banner reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Spinner:
    frames: tuple[str, ...]
    ascii: tuple[str, ...]
    interval_ms: int = 100


@dataclass(frozen=True)
class Theme:
    """The §12 dataclass. `load`/`degrade` are added in WP-8.3; the data is complete now so
    that work extends this rather than reshaping it."""

    name: str
    color: Mapping[str, str]
    gradient: Mapping[str, Sequence[str] | Sequence[int]]
    glyph: Mapping[str, str]
    glyph_ascii: Mapping[str, str]
    spinner: Spinner
    logo: Sequence[str]
    intro: str = "selvedge"
    max_fps: int = 15


#: The built-in "loom-dark". Values are §12 verbatim, except the logo: DECISIONS.md (2026-08-31)
#: picks mark 1a, plain-weave notation, over the 1b shuttle the drawing is rendered with. 1a's
#: single-char form is ▚ (U+259A), kept in `glyph["logo"]`; the selection cursor stays ❯.
DEFAULT_THEME = Theme(
    name="loom-dark",
    color={
        "ground": "#21201e",
        "inset": "#2a2825",
        "edge": "#3a3733",
        "fg": "#e8e4dc",
        "dim": "#958d84",
        "faint": "#605a53",
        "accent": "#cf9a4c",
        "accent_dim": "#8a6a34",
        "ok": "#8a9a63",
        "fail": "#c26a52",
        "info": "#6f9490",
        "selection": "#3a3226",
    },
    gradient={
        # the FR-ANIM-03 effort track, Faster -> Smarter
        "effort": ["#6f9490", "#cf9a4c", "#e0b064", "#cf9a4c", "#6f9490"],
        "effort_256": [66, 137, 179, 215, 222],
        "sweep_ms": [5500],
    },
    glyph={
        "prompt": ">",
        "selected": "❯",
        "mode": "⏵⏵",
        "cursor": "█",
        "logo": "▚",
        "warp": "┆",
        "weft": "━",
        "validate": "◌",
        "plan": "≡",
        "design": "⊞",
        "build": "█",
        "ok": "✓",
        "fail": "✗",
        "hard_fail": "⊘",
        "cached": "⟳",
        "denied": "⊗",
        "queued": "·",
        "running": "▸",
        "unevaluated": "—",
        "slider_mark": "▲",
        "track_left": "◀",
        "track_right": "▶",
    },
    glyph_ascii={
        # NO_COLOR · TERM=dumb · not a TTY · --no-animation
        "selected": ">",
        "mode": ">>",
        "ok": "[ok]",
        "fail": "[x]",
        "hard_fail": "[!!]",
        "denied": "[no]",
        "slider_mark": "^",
    },
    spinner=Spinner(
        frames=("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"),
        ascii=("|", "/", "-", "\\"),
        interval_ms=100,
    ),
    logo=("▀▄▀▄▀", "▄▀▄▀▄", "▀▄▀▄▀"),
    intro="selvedge",
    max_fps=15,
)
