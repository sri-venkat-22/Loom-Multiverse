"""The theme, as data — the drawing's §12 turned into the shape `FR-ANIM-05` requires.

Every colour, glyph, spinner frame, logo line and gradient stop lives here and nowhere else, so
a widget never hard-codes one (CLAUDE.md invariant). `.loom/theme.toml` overriding these defaults
(`load`) and the truecolor→ascii degradation ladder (`degrade`) are `FR-ANIM-05`/`FR-ANIM-04`,
which land with the live output in WP-8.3 — WP-8.1 needs only the defaults the banner reads.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

#: The §12 theme.toml top-level tables. A file with any other top-level key is refused loudly
#: (the drawing's own words), the same contract `config.Config` gives `.loom/config.toml`.
_TOML_KEYS = frozenset({"name", "color", "gradient", "glyph", "spinner", "logo", "limits"})


class ThemeError(ValueError):
    """A `.loom/theme.toml` that names a table or key the schema does not define."""


@dataclass(frozen=True)
class Spinner:
    frames: tuple[str, ...]
    ascii: tuple[str, ...]
    interval_ms: int = 100


@dataclass(frozen=True)
class Theme:
    """The §12 dataclass. `load` overlays `.loom/theme.toml`; `degrade` resolves it for a
    terminal that cannot show the Unicode kit (FR-ANIM-05/04)."""

    name: str
    color: Mapping[str, str]
    gradient: Mapping[str, Sequence[str] | Sequence[int]]
    glyph: Mapping[str, str]
    glyph_ascii: Mapping[str, str]
    spinner: Spinner
    logo: Sequence[str]
    intro: str = "selvedge"
    max_fps: int = 15

    def glyph_for(self, name: str, *, ascii: bool = False) -> str:
        """One glyph, ascii twin substituted when asked and one exists (FR-ANIM-04)."""
        if ascii and name in self.glyph_ascii:
            return self.glyph_ascii[name]
        return self.glyph.get(name, "")

    def degrade(self, level: str) -> Theme:
        """Resolve the glyph kit for a capability `level`. At ``ascii`` — TERM=dumb, a pipe,
        --no-animation — every glyph with an ascii twin is swapped and the spinner uses its
        ascii frames; the drawing's §12 keeps Unicode at every higher level (colour is dropped
        at render time, not baked into the glyphs), so those return the theme unchanged."""
        if level != "ascii":
            return self
        return replace(
            self,
            glyph={**self.glyph, **self.glyph_ascii},
            spinner=Spinner(self.spinner.ascii, self.spinner.ascii, self.spinner.interval_ms),
        )

    @classmethod
    def load(cls, root: Path | str, *, base: Theme | None = None) -> Theme:
        """`.loom/theme.toml` overlaid on `base` (the built-in default), or `base` if there is
        no file. FR-ANIM-05: swapping the stakeholder's future assets is a data change — dropping
        a file with new spinner frames must not require a code edit. Unknown tables are refused."""
        base = base or DEFAULT_THEME
        path = Path(root) / ".loom" / "theme.toml"
        if not path.is_file():
            return base
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        return _overlay(base, data)


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


def _overlay(base: Theme, data: Mapping[str, Any]) -> Theme:
    """Merge a parsed theme.toml onto `base`. Each table overrides only the keys it names, so a
    file that swaps just the spinner frames keeps every colour and glyph of the default."""
    unknown = set(data) - _TOML_KEYS
    if unknown:
        raise ThemeError(
            f"unknown theme table(s) {sorted(unknown)}; the schema is {sorted(_TOML_KEYS)}"
        )
    glyph_tbl = dict(_table(data, "glyph"))
    glyph_ascii = {**base.glyph_ascii, **_table(glyph_tbl.pop("ascii", {}))}
    glyph = {**base.glyph, **{k: v for k, v in glyph_tbl.items() if isinstance(v, str)}}
    track = glyph_tbl.get("track")
    if isinstance(track, list):  # §12 spells the two arrows as one pair
        glyph.update(track_left=str(track[0]), track_right=str(track[1]))

    logo_tbl = _table(data, "logo")
    if isinstance(logo_tbl.get("glyph"), str):
        glyph["logo"] = logo_tbl["glyph"]

    spin = _table(data, "spinner")
    spinner = Spinner(
        frames=tuple(spin.get("frames", base.spinner.frames)),
        ascii=tuple(spin.get("ascii", base.spinner.ascii)),
        interval_ms=int(spin.get("interval_ms", base.spinner.interval_ms)),
    )
    return Theme(
        name=str(data.get("name", base.name)),
        color={**base.color, **_table(data, "color")},
        gradient={**base.gradient, **_gradient(_table(data, "gradient"))},
        glyph=glyph,
        glyph_ascii=glyph_ascii,
        spinner=spinner,
        logo=list(logo_tbl.get("mark", base.logo)),
        intro=str(logo_tbl.get("intro", base.intro)),
        max_fps=int(_table(data, "limits").get("max_fps", base.max_fps)),
    )


def _table(data: Mapping[str, Any], key: str = "") -> dict[str, Any]:
    """A sub-table as a dict — `data` itself when `key` is empty, else `data[key]` if it is a
    table, else empty. Keeps a scalar written where a table belongs from crashing the merge."""
    value = data.get(key) if key else data
    return dict(value) if isinstance(value, Mapping) else {}


def _gradient(tbl: Mapping[str, Any]) -> dict[str, Any]:
    """§12 writes `sweep_ms = 5500`, a scalar; the in-code shape is a sequence so the field type
    stays uniform. Wrap any bare number so both forms load."""
    return {k: ([v] if isinstance(v, (int, float)) else v) for k, v in tbl.items()}
