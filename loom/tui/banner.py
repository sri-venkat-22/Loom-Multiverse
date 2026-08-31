"""The launch banner — SRS §3.2, drawn in §03 of the terminal design.

`render_banner` is a pure function of `BannerState` (FR-CLI-02). The state carries where the
credential came from but never the credential itself (FR-CLI-03), so the key cannot leak here by
construction. Glyphs and the logo come from the theme, never literals (CLAUDE.md invariant).
"""

from __future__ import annotations

from dataclasses import dataclass

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


def _context(tokens: int | None) -> str:
    if tokens is None:
        return ""
    size = f"{tokens / 1_000_000:g}M" if tokens >= 1_000_000 else f"{tokens // 1000}k"
    return f" ({size} context)"


def render_banner(state: BannerState) -> str:
    g = state.theme.glyph
    l0, l1, l2 = state.theme.logo
    return "\n".join(
        [
            f"  {l0}   Loom v{state.version}",
            f"  {l1}   {state.model}{_context(state.context_tokens)} with "
            f"{state.effort} effort · {state.billing} · {state.provider}",
            f"  {l2}   {state.cwd}",
            f"          key from {state.credential_source}",
            "",
            *(f"  {line}" for line in INVITATION.splitlines()),
            "",
            state.run_state.rjust(WIDTH),
            f"{g['prompt']} {g['cursor']}",
            "─" * WIDTH,
            f"  {g['mode']} {state.mode} mode on (shift+tab to cycle) · "
            "@ for files · ! for bash · # to remember",
        ]
    )
