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
