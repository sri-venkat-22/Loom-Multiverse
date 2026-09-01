"""The launch banner — SRS §3.2, drawn in §03 of the terminal design.

`render_banner` is a pure function of `BannerState` (FR-CLI-02). The state carries where the
credential came from but never the credential itself (FR-CLI-03), so the key cannot leak here by
construction. Glyphs and the logo come from the theme, never literals (CLAUDE.md invariant).
"""

from __future__ import annotations

from dataclasses import dataclass

from loom.tui.anim import Capability, gradient_track, paint
from loom.tui.theme import Theme

#: Frame width. SRS §3.2 draws the rule and right-aligns the run state to this column.
WIDTH = 78

#: Verbatim from SRS §3.2 — copy is not paraphrased (design §00).
INVITATION = (
    "Describe what you want built and Loom will validate it, plan it, design it,\n"
    "then write and test the code. Type / for commands."
)


@dataclass(frozen=True)
class BannerState:
    """Everything the banner shows, resolved. Assembled by the CLI (business logic), rendered
    here (presentation) — the split SRS §2.5 requires of `loom/tui/`."""

    version: str
    model: str
    context_tokens: int | None
    effort: str
    billing: str  # "BYOK" in R1; the account plan in R2
    provider: str
    cwd: str
    credential_source: str  # where the key came from — never the key (FR-CLI-03)
    run_state: str
    mode: str
    theme: Theme
    cap: Capability = "no_color"  # colour rung, resolved by the CLI; default renders plain


def _context(tokens: int | None) -> str:
    if tokens is None:
        return ""
    size = f"{tokens / 1_000_000:g}M" if tokens >= 1_000_000 else f"{tokens // 1000}k"
    return f" ({size} context)"


def render_banner(state: BannerState, *, include_input: bool = True) -> str:
    """The launch frame — SRS §3.2, design §03.

    `include_input` draws the prompt line, the gradient rule and the mode hint below the header.
    The still (a piped or non-TTY run) keeps it so the frame is complete; the interactive session
    passes ``False`` and lets the live REPL own those three — its pulsing prompt is the real input
    and its cursor is where typing lands, so drawing a second, static prompt here (the old banner)
    is what put the cursor above the line you actually typed on.
    """
    g = state.theme.glyph
    l0, l1, l2 = state.theme.logo

    def p(text: str, color: str) -> str:
        """Paint one whole segment — never mid-word, so every field stays greppable."""
        return paint(text, str(state.theme.color[color]), state.cap)

    lines = [
        f"  {p(l0, 'accent')}   {p('Loom v' + state.version, 'fg')}",
        f"  {p(l1, 'accent')}   "
        + p(
            f"{state.model}{_context(state.context_tokens)} with "
            f"{state.effort} effort · {state.billing} · {state.provider}",
            "dim",
        ),
        f"  {p(l2, 'accent')}   {p(state.cwd, 'dim')}",
        f"          {p('key from ' + state.credential_source, 'faint')}",
        "",
        *(f"  {p(line, 'fg')}" for line in INVITATION.splitlines()),
        "",
        p(state.run_state.rjust(WIDTH), "dim"),
    ]
    if include_input:
        lines += [
            f"{p(g['prompt'], 'accent')} {p(g['cursor'], 'accent')}",
            gradient_track(state.theme, WIDTH, state.cap),
            f"  {p(g['mode'], 'accent')} "
            + p(
                f"{state.mode} mode on (shift+tab to cycle) · "
                "@ for files · ! for bash · # to remember",
                "faint",
            ),
        ]
    return "\n".join(lines)
