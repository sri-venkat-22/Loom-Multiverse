# CODING AGENTS: READ THIS FIRST

This is a **handoff bundle** from Claude Design (claude.ai/design).

A user mocked up designs in HTML/CSS/JS using an AI design tool, then exported this bundle so a coding agent can implement the designs for real.

## What you should do — IMPORTANT

**Read `design/terminal/project/Loom Terminal Interface.dc.html` in full.** The user had this file open when they triggered the handoff, so it's almost certainly the primary design they want built. Read it top to bottom — don't skim. Then **follow its imports**: open every file it pulls in (shared components, CSS, scripts) so you understand how the pieces fit together before you start implementing.

**If anything is ambiguous, ask the user to confirm before you start implementing.** It's much cheaper to clarify scope up front than to build the wrong thing.

## About the design files

The design medium is **HTML/CSS/JS** — these are prototypes, not production code. Your job is to **recreate them pixel-perfectly** in whatever technology makes sense for the target codebase (React, Vue, native, whatever fits). Match the visual output; don't copy the prototype's internal structure unless it happens to fit.

**Don't render these files in a browser or take screenshots unless the user asks you to.** Everything you need — dimensions, colors, layout rules — is spelled out in the source. Read the HTML and CSS directly; a screenshot won't tell you anything they don't.

## Bundle contents

- `design/terminal/README.md` — this file
- `design/terminal/project/` — the `Terminal interface animations design` project files (HTML prototypes, assets, components)

---

## Where this fits in Loom

Exported 2026-08-30 from the Claude Design canvas
<https://claude.ai/design/p/967ea394-d8a3-4680-a9e4-b8fbbf5a2f5e?file=Loom+Terminal+Interface.dc.html>
(the canvas is the live source; this directory is a point-in-time export).

`project/Loom Terminal Interface.dc.html` is the drawing **WP-8.1, 8.2 and 8.3 get built from** —
every frame is annotated with the SRS requirement it satisfies. It resolves assumption **A-1** in
`docs/SRS.md` §2.6 ("the stakeholder will supply animation assets and a colour palette later").

Read it before starting any 8.x work package. Section §12, *Theme as data*, is the one that becomes
code directly: it is the full `.loom/theme.toml` schema and the `loom/tui/theme.py` dataclass shape
that `FR-ANIM-05` requires, plus the four-step degradation ladder for `FR-ANIM-04`.

`project/uploads/loom DOC .pdf` is reference 5 in `docs/SRS.md` §1.4 — the stakeholder's original
terminal-experience notes, and the source for SRS §3.
