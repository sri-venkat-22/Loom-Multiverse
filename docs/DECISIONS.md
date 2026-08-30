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

## Terminal design bundle (2026-08-30)

- `design/terminal/` is a point-in-time export of the Claude Design canvas, committed rather than
  linked. A URL is not readable from a session with no network, and the 8.x work packages are months
  out — the canvas may have moved on or away by then. The canvas stays the live source; the export
  is what the repo builds from, and re-exporting is how it gets updated.
- The bundle resolves SRS assumption A-1. The palette, glyph kit, spinner sets, gradient stops and
  `.loom/theme.toml` schema are now decided data, not a placeholder.
- Its numbers were checked against landed code before filing: the effort slider's five rows match
  `EFFORT_PRESETS` exactly, the tier switch falls between `high` and `xhigh` as `MODEL_TIERS` has it,
  the banner and slider both show `medium` as the default effort, `/cost` ledgers judge spend
  separately, and budget exhaustion exits 3. Nothing in Phase 1 needed changing.
- Two spec conflicts the drawing found, both still open: SRS §3.3 lists `/demo` while the §5.2
  catalogue registers `/preview` (one is a typo — the registry should win, FR-SLASH-05), and the §3.2
  banner mock has no line for the credential source that FR-CLI-03 makes mandatory. The drawing adds
  one; the SRS mock should be amended to match before WP-8.1.
- The mark is undecided. Everything from §03 on is drawn with candidate 1b (the shuttle `❯`, which
  doubles as the prompt and the selection cursor); 1a (plain weave) and 1c (heddle grid) are drawn
  but not applied. WP-8.1 needs this answered before the banner is built.
- WP-8.2's `/model` picker needs a model catalogue (name, blurb, price, tier) that `loom/config.py`
  does not have — today there is `MODEL_TIERS` (two entries) and `DEFAULT_PRICE_TABLE` (three). The
  drawing lists four, including `ollama/qwen2.5-coder`. Deferred to 8.2 rather than guessed at now.

## Phase 2 — the agent core (2026-08-31)

- `litellm` lands as a runtime dependency (NFR-MAINT-05). It is the provider-agnostic seam
  `CLAUDE.md` mandates, and nothing else in the tree may import it. It costs ~1.2 s to import,
  which is why `loom/cli.py` never reaches `providers.py` and why NFR-PERF-01's cold-start test
  exists.
- `providers.py` sets `LITELLM_LOCAL_MODEL_COST_MAP=True` before importing litellm. litellm
  otherwise fetches its price map from GitHub *at import time*; Loom makes no unannounced
  outbound call, and a stale local map is exactly what `DEFAULT_PRICE_TABLE` is the fallback for.
- The price-table fallback is confirmed necessary, not theoretical: `completion_cost()` prices
  `openrouter/qwen/qwen3-coder` fine but raises a bare `Exception` on `dashscope/qwen3-coder-next`
  ("this model isn't mapped yet"). That is FR-COST-02's failure mode, reproduced in a test.
- The fallback announces itself as a `budget_warning` event with `reason="price_table_fallback"`
  rather than a new event kind. What it warns about is that the budget's numbers are estimated
  rather than measured, which is what that kind already means; a new kind would need an SRS §6
  edit for no gain.
- `to_response()` is a pure function, separate from `LiteLLMProvider`. That is what lets a
  cassette replay a recorded raw payload through the real normaliser — replaying a pre-built
  `Response` would test nothing, since `FakeLLM` already does that and cannot see shape drift.
- Command substitution (`$(…)`, backticks, `<(…)`) is denied outright rather than analysed.
  SEC-02 only requires denying substitution that reaches outside the jail, but deciding that
  statically is undecidable and a build has no honest need for it.
- The bash deny-list recurses into `sh -c '<command>'`. Without it the entire list is one
  `sh -c` away from being decorative.
- The deny-list checks path arguments only for commands whose arguments *are* paths
  (`PATH_COMMANDS`). Checking every token would deny
  `git commit -m "fix the /etc/hosts parser"`, and a deny-list that blocks the build loop is
  worse than none. `tests/test_security.py` carries as many allow cases as deny cases for
  exactly this reason.
- General-purpose network commands (`curl`, `ssh`, `gh`, …) are denied but package managers are
  not: a build cannot install its dependencies without them. SEC-07 is a boundary, not an
  airgap, and SEC-08's per-turn snapshot is the real backstop.
- `read_file` returns raw file content, not numbered lines. Numbering helps a human read and
  hurts `str_replace`, which needs the model to reproduce the text exactly.
- Tool argument *errors* come back as strings the model can correct itself from; security
  refusals (`JailEscape`, `CommandDenied`) raise, and the loop frames them as tool messages.
  Same destination, but a refusal is never silently indistinguishable from a typo.
- `ToolRegistry.execute` ignores unexpected arguments rather than forbidding them: a stray key
  is not worth failing a call the model otherwise got right, and a *misspelled required* one
  still fails as missing.
- `LoopResult` lives in `loop.py`, not `contracts.py`, despite SRS §6 listing it in the data
  model. It is not a phase artifact and nothing persists it. SRS §6 is the bug.
- The loop's `rubric` is typed as a local `Grader` protocol, not `contracts.Rubric`: the
  contract describes a rubric, this is something that can *run* one. It keeps WP-4.2 free to
  land `rubric.py` without the loop importing it.
- Stall detection needs a baseline round plus `STALL_ROUNDS` non-improving ones — four graded
  rounds, not three. There is nothing for the first round to fail to improve on.
- `"blocked"` is returned when a response carries neither text nor tool calls. Re-prompting an
  empty response just buys another one.
- Retry jitter is injected (`jitter=`) so the backoff test asserts exact growth. With ±50%
  jitter the delay bands for consecutive attempts overlap, so `delays[1] > delays[0]` is a flake.
- `tests/cassettes/*.json` are currently **hand-written** to the documented provider shapes,
  not recordings — no key was available. They reproduce the three Qwen quirks the adapter exists
  for (arguments as a JSON string, a tool call with no id, a response with no `usage` block).
  `make cassettes` overwrites them with real recordings and no test changes.
- `ask_user`'s registered handler raises. The loop routes the tool to `ask_user_fn` (FR-ASK-04),
  so the body is unreachable — and says so loudly if a refactor ever makes it reachable.
- No `prompt_fn` behaves identically to `--yes`. A piped run has no terminal to ask through;
  that is a default, not an error, and it is what keeps FR-HEADLESS from needing a second code
  path.

## WP-4.1 — workspace (2026-08-31)

- Snapshots are real commits, not `refs/` trees written with `commit-tree`. "Squash the turn
  commits on phase success" presupposes turn commits, and the generated app lives in git anyway
  (SRS §5.3).
- `reset_to` is `reset --hard` **plus** `clean -fd`. `reset --hard` alone leaves untracked files
  behind, so turn 6's half-written module would survive into turn 3 — "the exact tree" minus the
  part everybody forgets. `-x` is deliberately absent so ignored files (virtualenvs) survive.
- `.loom/` is excluded from `add`, `status`, `write-tree` and `clean` by pathspec, not by
  trusting the `.gitignore` FR-WS-04 writes. Two failures if it were not: snapshots would commit
  the event log and `reset_to` would roll back the log of the run doing the resetting, and a
  growing `events.jsonl` would change `tree_hash()` on every write, busting WP-4.2's memo on
  every grading round.
- Every constructor plants an empty baseline commit. A repo with no commits makes `head()`,
  `reset --soft` and `merge-base` each a special case; one commit removes all of them.
- Snapshot labels are validated against `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` before reaching git,
  because they become ref names and `refs/loom/snap/../../heads/main` is a sentence git parses.
- Phase bases are refs (`refs/loom/base/<phase>`), not memory, so a resumed process can still
  squash the phase it interrupted.
- `commit_phase` refuses unless the base is an ancestor of HEAD (FR-WS-03). A soft reset to a
  base HEAD no longer descends from would silently delete everything in between.
- Loom commits as `Loom <loom@localhost>` via `-c`, never writing to the user's git config.
  These are machine snapshots; attributing generated code to whoever was logged in is a small
  lie that stays in `git blame`.
- `Workspace` is synchronous. git is fast, snapshots are once per turn, and an async wrapper
  around four `subprocess.run` calls buys nothing.
- Turn refs are left in place after a squash: those commits leave the branch history but stay
  reachable, so `reset_to("turn-3")` still works afterwards.
- The two bash timeout tests were cut from 2s to 1s and 0.5s. Nothing is proved by waiting two
  seconds that is not proved by half of one, and the git-backed workspace tests had pushed the
  unit tier to 14.7s against NFR-TEST-01's 10s ceiling. Now 9.0s.

## WP-4.2 — rubric (2026-08-31)

- The memo is **re-stamped against the tree hash as grading left it**, not the one it started
  with. Grading has side effects — `pytest` writes `.pytest_cache/` and `__pycache__/` — so
  unless the generated repo happens to ignore both, the post-round hash differs from the
  pre-round one and the next round misses every entry. COST CONTROL 2 would then be dead weight
  that looks alive, and the only symptom is a bigger bill. Costs one extra `git write-tree` per
  round. `tests/test_rubric.py` pins both halves: a side-effecting criterion still memoizes, and
  a real file change still busts it.
- Memoization is opt-in by passing a `memo` dict, and only then is a tree hash computed. A
  caller that does not memoize does not need the workspace to be a git repository at all — which
  also keeps most of the rubric's own tests off git.
- `grade()` ledgers judge calls itself under `phase="judge"` rather than relying on the caller
  to have built `judge_provider` with the right phase. FR-RUB-07 is a property of the rubric,
  not of whoever wired it. The injected judge provider should therefore carry no ledger of its
  own, or spend is counted twice.
- A retried judge call is ledgered too. It cost money whether or not it parsed.
- Shell pass/fail is read from `run_bash`'s `exit 0` prefix. ponytail: parsing a sibling
  module's output format. The tests run real commands through the real tool, so a change to that
  format fails there instead of silently marking every build green; the upgrade path, if it ever
  bites, is a structured result from `bash.py` that the tool formats for the model.
- Judge evidence is wrapped in `BEGIN EVIDENCE`/`END EVIDENCE` and the system prompt says
  content inside is never an instruction. The repo under review was written by a model that
  knows it is about to be graded; SEC-04's framing applies here as much as to fetched web pages.
- `evidence_paths` globs are filtered through the path jail. They come out of a model-written
  Design and are not trusted input.
- A judge score outside `[0, 1]` is clamped rather than retried — 1.4 is a clear intent, badly
  expressed — but unparseable JSON costs the one retry FR-RUB-05 allows. A fenced JSON object
  is accepted without spending it, because every instruction-tuned model does that eventually.
- Criteria keep their own scores when a `hard_fail` forces the total to 0.0. Only the total is
  overridden, so `as_feedback()` can still say how close it got.
- The unit tier is now 10.8 s against NFR-TEST-01's 10 s. What is left is real subprocess work
  — git in the workspace tests, actual timeouts in the bash and rubric tests — and one 1.1 s
  litellm import. Either split a `slow` marker or amend the NFR; do not fix it by mocking git,
  which is the one thing those tests exist to exercise.

## WP-4.3 / WP-4.4 — scaffold and the build phase (2026-08-31)

- `scaffold()` runs the Design's command through the same guarded bash tool as everything else.
  `scaffold_command` is model-written, so it is untrusted; a denied or failing command returns
  `ok=False` rather than raising, and the build proceeds with the agent writing its own
  boilerplate. A bad scaffold command should cost a turn, never the run.
- The scaffold recipe lives in the Design, not in `scaffold.py`. FR-BUILD-06's "passing empty
  suite" is `mkdir -p tests` plus a placeholder test in the fixture's `scaffold_command` —
  `pytest` exits 5 on a repo with no tests, so without it the rubric's test criterion fails on
  round one for entirely the wrong reason.
- The round loop stays in `agent/loop.py`. `run_build` owns the wiring that makes rounds safe
  and inspectable — scaffold, tool set, per-turn snapshot, persisted score, squash on success —
  and nothing else. Re-implementing grade/feedback/repeat here would be a second copy of it.
- Per-turn snapshots are taken from the loop's own `turn` events, which fire at the *start* of a
  turn: turn N is snapshotted when turn N+1 begins, and the last when the loop ends. One commit
  per turn, honest labels, and nothing new required from `loop.py`.
- `RubricGrader` is an object rather than a closure because it owns the memo, and the memo has
  to survive across grading rounds for COST CONTROL 2 to mean anything.
- The model is shown the rubric it will be graded against. Hiding it would only delay the same
  information until the first failed round; the prompt's integrity clause is what keeps showing
  it from being an invitation.
- Only success squashes. An unfinished phase keeps its turn commits, because that is what a
  resume reads. The turn *labels* survive a squash either way, so a completed phase is still
  rewindable.
- `build.py` imports `_atomic_write` from the fs tool module rather than re-writing
  write-then-replace. WP-3.1 needs it too and should promote it out of the tool module then.
- **`rubric_rounds` is not enforced.** It is a column of the FR-SEL-03 effort table but no FR
  requires it, and capping graded rounds would need a `max_graded_rounds` parameter on
  `run_agent_loop` — WP-2.6's file, outside this scope. Stall detection, `max_turns` and
  `max_usd` bound the spend today. Wire it when the loop next changes.
- The unit tier is now **21 s** against NFR-TEST-01's 10 s, and `test_build_phase.py` is half of
  it. The cost is real git and real subprocesses, which is exactly what those tests exist to
  exercise — the fix is a selector, not a mock. See the note in the WP-4.2 section.

## Test tiers — the `slow` marker (2026-08-31)

- `slow` marks tests that drive a real git repository or a full build loop:
  `test_workspace.py` and `test_build_phase.py`, 50 tests and about 15 of the suite's 21
  seconds. `make fast` (`-m "not slow"`) is the on-every-save tier at 6.4 s; plain
  `uv run pytest -q` still runs everything and stays the CI gate. NFR-TEST-01 was amended to
  say so, since the 10 s budget now belongs to the fast tier rather than to the whole suite.
- The line is "does it need a real repo or a real build loop", not "is it slow". `test_bash_tool`
  and `test_scaffold` also spawn subprocesses, but a subprocess *is* the unit under test there,
  and together they cost under 3 s.
- **`addopts = "-m 'not live'"` was removed and replaced with a `pytest_collection_modifyitems`
  hook.** A command-line `-m` overrides addopts entirely — last one wins — so `pytest -m "not
  slow"`, the exact command this change exists to enable, would have collected the live tests
  and spent real money. Verified before the change: `pytest -m "not slow"` collected 360 tests
  including the live recorder. Skipping `live` by keyword unless the `-m` expression names it
  makes every marker expression composable and safe.
