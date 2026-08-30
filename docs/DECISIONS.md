# Decisions

One line per non-obvious choice, appended by whoever made it.

- WP-0 (spike) skipped for now — the four numbers (turns-to-green, malformed tool-call rate,
  USD/run, whether `litellm.completion_cost()` prices our model strings) are still unknown.
  Record them here before WP-2.6/2.7a hard-code any default. `max_turns`/`max_usd` defaults in
  `loom/config.py` are placeholders until then.
- `ToolCall.arguments` coerces a JSON *string* into a dict inside the contract, not in
  `providers.py`. Qwen emits both shapes; one validator at the type boundary beats one per adapter.
- `Ledger` carries a `run_id` column that the plan's schema does not list, because WP-5.1's
  `--budget` is a per-run ceiling and adding the column later is a migration.
- Config file format is TOML via stdlib `tomllib` (3.11+), so no parser dependency.
- Env override keys are `LOOM_<FIELD>` and are parsed by Pydantic, so `LOOM_MAX_TURNS=40`
  coerces to int without hand-written casts.
- `load_config` takes `cwd`/`home`/`env` as arguments instead of reading globals, so the four
  precedence levels are testable without monkeypatching `Path.home`.
- `read_events` skips any line that does not parse, not just the trailing one — a torn write
  mid-file is the same failure and the same fix.
- `docs/SRS.md` amends two `CLAUDE.md` invariants rather than breaking them: terminal libraries are
  allowed in `loom/cli.py`, `loom/gates.py`, `loom/ui.py` and the new `loom/tui/` only (the core stays
  TTY-free and library-drivable), and "no web UI, no server" holds for R1 while R2's account service
  lives in a separate package the CLI reaches through `loom/account.py` alone. `CLAUDE.md` must carry
  the new wording before WP-8.1 starts.
- TUI input layer is `prompt_toolkit`, not `questionary` (a thin wrapper that cannot render the inline
  palette/slider) and not `textual` (full-screen, which would destroy scrollback).

## SRS v1.0 alignment (2026-08-30)

- Default effort is `medium`, not `high`. The landed defaults (40 turns, $2.00/phase) were `high`'s
  numbers by coincidence; `medium` is what SRS §3.4's slider example shows, and it is the only level
  where four phases at the per-phase ceiling still fit inside the default `budget_usd` of $5.
  Revisit when WP-0's numbers (Q-1) say what a real build actually costs.
- At `xhigh`/`max` the per-phase ceiling deliberately exceeds `budget_usd`; the per-run ceiling is
  the backstop and is meant to bind first. `budget_usd` is not effort-derived — FR-SEL-03's table
  does not have a column for it, and a run ceiling that grows itself is not a ceiling.
- `Config` field defaults are the `DEFAULT_EFFORT` preset's values, with a `mode="before"` validator
  filling `max_turns`/`max_usd`/`rubric_rounds`/`model` from the preset for keys no layer set. One
  rule in one place covers both `Config(...)` and `load_config(...)`, and explicit still beats preset
  at every layer (FR-SEL-03).
- `rubric_rounds` is a `Config` field even though SRS §6's key list omits it: it is a column of the
  FR-SEL-03 preset table, so it needs somewhere to land, and the same override rule should apply.
- The default `price_table` carries the three models Loom names, priced from SRS §3.4 as of
  2026-08-30. Indicative, not authoritative — but a stale price beats FR-COST-02's failure mode,
  which is raising after the tokens are already paid for.
- `loom/session.py` closes the event vocabulary (`EVENT_KINDS`, SRS §6) and refuses an unknown kind.
  Same reasoning as `extra="forbid"` on config: a typo'd kind is invisible to `loom status` and to
  `/resume`, so it should fail at the call site. Adding a kind is one line.
- `Session.interruptible()` records `interrupted` and re-raises `KeyboardInterrupt` for SIGTERM as
  well as SIGINT (FR-SESS-09) — Python has no other honest "unwind now" exception — restoring the
  previous handler first so a second signal during cleanup behaves normally. Off the main thread it
  is a no-op, because `signal.signal` cannot be called there at all.
- `ExitCode` (FR-CLI-06) lives in `loom/cli.py` for now, since only the CLI raises them. It moves to
  a core module the day `loom/pipeline.py` needs to signal "rubric not met" without importing typer.
- A malformed config exits `2` (bad usage), not `1` (unexpected error) — the code did nothing
  unexpected, the config did.
- `tests/test_invariants.py` checks C-2/C-3 by parsing `loom/**/*.py` with `ast` rather than by
  importing modules with `sys.modules` masked. It works on files that do not exist yet, has no import
  side effects, and covers every future module for free. NFR-MAINT-02's suggested masking approach
  would only cover the modules that already import cleanly.
- `tests/conftest.py` blocks outbound sockets for every test except `@pytest.mark.live` (NFR-TEST-02).
  Listening and local IPC are untouched; only `connect` is refused.
