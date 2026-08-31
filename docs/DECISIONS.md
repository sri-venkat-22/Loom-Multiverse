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

## The urlshortener fixture Design (2026-08-31)

*Plan §10 lists this as not-to-delegate. Written on request; the judgment below is what wants
reviewing, not the JSON.*

- **A sufficient spec names the interface, not the intention.** Every `purpose` in the manifest
  gives signatures, return types and the errors raised — `resolve(code, store=None) -> str`
  "records exactly one click each time it is called", `stats` "records no click". A purpose like
  "handles storage" produces code that handles storage in whatever way the model felt like, and
  then the rubric is grading a coin toss. This is the property `prompts/design.md` will be tuned
  to reproduce (FR-DES-04), so it is the fixture's most load-bearing decision.
- **At least one hard-fail criterion must be one the model does not write.** `behaviour` shortens
  a URL, resolves it twice and asserts the click count is exactly 2. If the only hard fail were
  "the model's own test suite passes", weakening a test would be enough to clear the rubric and
  FR-BUILD-04's prompt clause would be the only thing standing in the way. A prompt is not an
  enforcement mechanism. `tests/test_fixtures.py` asserts this property of every fixture.
- Stdlib-only (`sqlite3`), with `pytest` installed by the scaffold. The milestone measures
  whether a cheap model can build a repo; turns spent on dependency resolution are measuring
  something else, at $1 a run.
- The acceptance command starts with `rm -f .acceptance.db`. Without it the database survives
  between grading rounds, the click count accumulates, and a hard-fail criterion starts failing
  on round two for a reason that is nobody's fault.
- Six files and five criteria is the scope ceiling: enough that the manifest DAG, both criterion
  kinds and the hard-fail path all get exercised, small enough to fit 25 turns and $1.
- `tests/test_fixtures.py` runs every shell criterion through the deny-list, compiles the inline
  Python in each one, and checks each judge's evidence globs against the manifest. All four
  guards were verified against a deliberately broken copy before being trusted. The failure
  these prevent is a live run that costs a dollar and fails for a reason unrelated to the thing
  being measured.
- Verified by running the real `scaffold()` against the real command: `uv run pytest -q` exits 0
  on the placeholder suite, `import shortener` resolves, and `.venv` stays out of git because
  `uv init` writes a `.gitignore` covering it — so per-turn snapshots stay small. `.pytest_cache`
  is *not* in that `.gitignore`, which is precisely the case WP-4.2's memo re-stamp handles.

## WP-4.5 — the milestone harness (2026-08-31)

- **The harness is written; the milestone is not met.** Nothing here has run against a real
  model — there is no key on this machine. `make milestone` is the command; the number it
  produces is the actual WP-4.5 result and belongs in this file when it exists.
- The test prints a full report on **both** paths, and `run_build` is wrapped so a provider that
  dies on turn 30 still prints what turn 29 bought before re-raising. A failed milestone that
  costs a dollar and yields only a traceback has wasted most of what it cost. The report carries
  status, score per criterion, turns, spend by phase, the files produced, tool-call counts,
  retries, and the path to a JSONL event trace.
- Model, judge, budget and turn cap are environment variables (`LOOM_E2E_*`), so comparing Qwen
  against Sonnet is a shell variable rather than an edit. Defaults are the cheap tier and the
  $1.00 the plan sets.
- `ask_user_fn=AskUser(yes=True)`: a question with nobody to answer it would hang the milestone.
- The judge provider is built without a ledger of its own. `grade()` records judge spend itself
  under `phase="judge"`, and a second ledger would double-count it.
- Credentials are read from `~/.loom/credentials.json` as well as the environment (FR-CFG-06),
  but the check that does it can never raise at import: this module is imported by every
  `pytest` run, so a wrongly-permissioned credentials file would otherwise break `make check`
  for the whole suite. It degrades to a skip reason instead.
- A non-live `test_the_milestone_harness_is_wired_before_anyone_pays_for_it` asserts the parts
  that can silently make the milestone unpassable — the threshold, the budget, that the
  criterion the anti-gaming assertion names still exists, and that the model string maps to a
  known API key variable.
- Verified end to end against a deliberately invalid key: the fixture loads, the workspace is
  created, the scaffold produces `pyproject.toml`, `src/shortener/__init__.py` and the
  placeholder suite, the tool registry is built, the loop starts, and a 401 surfaces
  immediately rather than being retried (401 is correctly absent from `RETRY_STATUS`). That run
  also found a `NoneType` bug in the failure-path report, which is the reason to smoke a
  reporting path rather than trust it.

## Phase 3 — the Shape A phases, gates, pipeline and replay (2026-08-31)

- **`phases/base.py` runs the agent loop once and then repairs with plain provider calls.** A
  repair re-uses the transcript and appends the validation error as a user message, so a
  three-attempt phase costs three calls rather than three agent loops. It is also what makes
  FR-SESS-05's "exactly one provider call" true for a tool-free phase.
- **The output schema is generated into the system prompt, not requested through the provider's
  native structured-output mode.** Native mode is per-provider and conflicts with tool use on
  most of them, and Validate needs tools. Marked `ponytail:` in `base.py` with the hook
  (`LiteLLMProvider(extra=...)`) for the day the repair rate shows up in the eval harness.
- **`Phase.refine()` does two different things on purpose.** Rewriting is silent (Plan moves an
  over-scoped feature to `non_goals` — which five of seven are the v0 is a call the model made
  by ordering them, and a repair round to re-make it is money for nothing). Raising `ValueError`
  lands in the repair loop (Design's rubric checks — a rubric that cannot be graded has to go
  back). `ValidationError` is caught before `ValueError` because pydantic's is a subclass.
- **FR-DES-03 is enforced in `refine`, not in `contracts.py`.** "Weights sum to 1.0" and "some
  shell criterion runs the tests" are true of a *good* rubric, not of every `Rubric` — the
  fixtures in `test_rubric.py` legitimately have neither. `test_design.py` asserts the
  hand-written fixture clears the gate, so a rule that is wrong fails a test rather than a run.
- **Test-runner detection is a substring match against a six-entry tuple.** A parser per runner
  is a subsystem; a false negative costs one repair round, a false positive costs a build that
  was never really tested — so the list errs toward being asked again.
- **Subclass `prepare_input` keywords are all optional, with an explicit check.** Required
  keywords narrow the base signature and mypy is right to refuse them. The guard turns a
  misuse into "the plan phase needs a Validation artifact" instead of a `TypeError`.
- **The web tools take an injected `fetcher`.** Every unit test in the project keeps its promise
  not to open a socket, including the ones about page parsing.
- **`search_web` scrapes one HTML endpoint with a regex** rather than adding a keyed search API.
  A signup form between a user and their first run is worse than a parser that will need
  fixing; the upgrade path is a key in `config.toml`. Marked `ponytail:` at the call site.
- **The web tool refuses private, loopback and link-local addresses.** The URL a phase fetches
  can come from a page a phase already fetched, so "the model chose it" is not a trust
  argument, and `169.254.169.254` is one hop away. The residual DNS-rebinding window is named
  in the docstring rather than closed; closing it needs a custom connector.
- **`SEC-04`'s delimiter, its note and the system clause are one set of constants in `web.py`.**
  A prompt promising framing that the tools do not apply is worse than no promise, so the tool
  and the clause cannot be edited apart.
- **The run budget is a `Provider` decorator (`BudgetGuard`) over one shared `RunBudget`.**
  FR-PIPE-03 says "before every provider call across all phases"; a decorator makes that
  structural instead of four call sites remembering to add up. `.model` is passed through
  because the phase cache keys on it.
- **`budget.check()` also runs before each phase, not only before each call.** A phase handed a
  ceiling of zero exits `budget_exhausted` without ever calling a provider, and the run would
  otherwise report "invalid artifact" for what is plainly an empty wallet.
- **`PipelineResult.phases_run` records a phase that produced an artifact, approved or not.**
  `loom resume` reads the artifacts on disk; a `phases_run` that disagreed with them would be a
  lie in every report.
- **A rejection is appended to the phase input; an edit skips the re-run entirely.** Showing the
  model its own rejected artifact is how you get the same artifact back with nicer wording, and
  paying a model to reproduce a decision a human just made is waste. Rejections are capped at
  two: past that the answer is `edit`, not a third invoice.
- **`run_build` gained a `feedback` parameter** so the last gate behaves like the other three.
  It is appended to the task, not to the design: the design was already approved, and a
  rejected build is a complaint about the code.
- **`loom replay` bypasses the cache and never overwrites.** An unchanged prompt would otherwise
  serve the old answer and make zero calls, which is the opposite of the point; and comparing
  two prompts is worthless if the second run destroys the first's output. Slots are
  `design.replay-N.json`, N one past the highest present, so deleting one does not make the
  next replay overwrite a comparison someone still has open.
- **`_atomic_write` was promoted to `atomic_write`** in `agent/tools/fs.py` rather than moved to
  a new module. Three importers now, and a new file for eight lines is worse than a public name
  in the module whose test already covers it.
- **`config.apply_credentials()` replaced the copy in `tests/e2e/test_build_urlshortener.py`.**
  One implementation of "credentials file into the environment", and `loom run` needs it too.
- **WP-4.7's e2e prints every artifact in full on both paths.** When a two-dollar run fails, the
  question is which phase wrote the thing that made the rest impossible, and nothing else
  answers it. Its non-live `test_the_harness_is_wired_before_anyone_pays_for_it` checks the
  acceptance threshold, the budget and the failure-path report for free.
- **The Design phase's own rubric threshold is asserted in WP-4.7**, not just the score. A phase
  that writes its own bar can pass by lowering it; the acceptance number is ours.
- **A gated run with no TTY is refused, not auto-approved.** Auto-approving would be the
  friendlier default and the wrong one: nobody piping into a command expects it to spend money
  unsupervised because it could not find them. `--yes` is the explicit way to say so.

## First-run credentials (2026-08-31)

- **Found by actually running `loom validate` with no key: it printed a litellm traceback.**
  Every first-time user would hit that on their first command, and a traceback reads as "this
  tool is broken" rather than "you have not set a key". `ProviderAuthError` now names the exact
  environment variable and the credentials-file path, and `cli.py` catches `ProviderError` and
  exits 2. The mapping lives in `providers.py` — the one module allowed to know litellm's
  exception shapes — so `cli.py` still imports no provider SDK.
- **An auth failure is never retried.** Four attempts at a key that is missing is four times the
  wait for the same answer. 401/403 skip the backoff entirely.
- **`litellm.suppress_debug_info = True`** at import: litellm prints its provider list and a
  feedback link to stderr on every error, on top of a message Loom has already phrased.
- **`KEY_FOR_PREFIX` moved from the WP-4.5 e2e into `providers.py`**, so the skip reason and the
  runtime error name the same variable. `test_providers.py` asserts every model in `MODEL_TIERS`
  maps to one — a default model whose key we cannot name gives the unhelpful message.
- **`nvidia_nim` added to `KEY_FOR_PREFIX`**, and a *pre-flight* check added alongside the 401
  path. Providers disagree about what a **missing** credential is: OpenRouter says 401, NVIDIA
  NIM says 500 — which is in `RETRY_STATUS`, so without the pre-flight it backed off four times
  for a reason no amount of waiting fixes. The pre-flight only runs when `acompletion` was not
  injected, so tests and cassettes are untouched. Found by pointing the model checker at a
  provider nobody had tried yet, which is the argument for having the checker.
- **A model with no entry in the price table costs $0.00, and that is load-bearing, not
  cosmetic.** `--budget`, `max_usd` and the `budget_exhausted` exit all read zero spend, so
  `max_turns` becomes the only ceiling that binds. Fine for a free endpoint, dangerous if
  anyone assumes the dollar caps still work. Asserted in `test_providers.py`.
- **The cheap tier is now `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`.** Chosen against
  OpenRouter's model API rather than a blog post: it advertises `tools` and `tool_choice`, which
  is the one capability that decides whether a model can drive the build phase, plus a 1M
  context. It does *not* advertise `response_format` / `structured_outputs` — the paid variant
  does — which costs nothing here only because `phases/base.py` puts the schema in the prompt.
  Had WP-3.1 gone the native-structured-output route, this model would not have worked at all.
- **Both variants are in `DEFAULT_PRICE_TABLE`, the free one at 0.0 on purpose.** An absent
  model and a free one both cost $0.00 through `_fallback_cost`; the entry is how the ledger
  says "this is free" rather than "we have no idea". The paid variant is listed so switching off
  `:free` gives real numbers with no further edit.
- **Two tests asserted Qwen's numbers where they meant an invariant** — that every tier is
  priced, and that the judge stays on the pinned cheap tier whatever `--model` says. Rewritten
  to assert those, so the next model swap does not fail them spuriously.
