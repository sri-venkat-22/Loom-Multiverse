# Loom

CLI that takes a founder's idea and produces a working, tested codebase.
Pipeline: Validate -> Plan -> Design -> Build+Test.
Requirements: `docs/SRS.md`. Work packages: `loom-agent-build-plan-v2.md`.

## Invariants — do not violate without asking
- Python 3.11+. No Node dependency, ever.
- `loom/` core must never import a terminal library — `typer`, `rich`, `prompt_toolkit`,
  `questionary`, or any other. Terminal concerns live in `loom/cli.py`, `loom/gates.py`,
  `loom/ui.py` and `loom/tui/` only. The core must stay drivable as a library, with no TTY.
- `loom/tui/` is presentation only: it renders state and returns choices, never business logic.
  Every path it drives must also be reachable by flag, and `--yes` must complete a full run with
  no TTY present. That equivalence is what keeps the boundary honest.
- R1 is terminal only — no web UI, no server. R2's account service is a separate package
  (`loom-cloud/`), reached from the CLI through `loom/account.py` and nothing else. BYOK mode
  must keep working with that service unreachable.
- Provider-agnostic: all model calls go through `litellm`, and every one of them goes through
  `loom/agent/providers.py`. No `litellm`, `anthropic`, or `openai` import anywhere else.
- Shape A phases (validate/plan/design) get NO filesystem or bash tools. This is a security
  boundary, not a style choice — those phases ingest untrusted web content.
- Every phase output is a Pydantic model from `loom/contracts.py`, persisted to
  `.loom/artifacts/`. Downstream phases read artifacts, never upstream transcripts.
- Editing existing files is `str_replace` (exact, unique-match-or-error). No diff-hunk apply.
- Never write secrets into the workspace. Never `git push`. Never `rm -rf`.

## Commands
- Test (unit + cassette): `uv run pytest -q`
- Live tests: `uv run pytest -m live`
- Lint: `uv run ruff check . && uv run ruff format --check .`
- Types: `uv run mypy loom`
- All: `make check`

## Style
- Pydantic v2. `from __future__ import annotations`. Full type hints.
- Prefer stdlib. New dependencies need a line in docs/DECISIONS.md.
- No `print()` in the core — structured events via `session.log_event`. Only `loom/cli.py`,
  `loom/ui.py`, `loom/gates.py` and `loom/tui/` write to the terminal, and everything they show
  must also be an event, so a piped or non-TTY run loses nothing.
- Tests use `FakeLLM` (loom/testing/fake_llm.py). No network in unit tests.

## Cost rules
- The judge model for rubric grading is pinned cheap in config and its spend is ledgered
  under phase "judge", separately from the phase that triggered it.
- Shell criteria grade before judge criteria. A failed hard_fail shell criterion returns
  immediately without spending a single judge call.

## Layout
loom/{cli,config,contracts,session,gates,ledger,scaffold,security,workspace,rubric,cache}.py
loom/{pipeline,replay,ui,diagnostics}.py  loom/account.py (R2)
loom/agent/{loop,context,providers}.py  loom/agent/tools/{fs,bash,web,ask_user,registry}.py
loom/phases/{base,validate,plan,design,build}.py
loom/tui/{app,banner,repl,commands,widgets,complete,live,theme,anim,permissions}.py
loom/testing/fake_llm.py  loom/prompts/*.md  loom/blueprints/*/

## Working agreement
- Stay inside the file scope given in the task. Ask before touching anything else.
- Write the test first, then the implementation.
- Append one line to docs/DECISIONS.md for any non-obvious choice.
- `docs/SRS.md` is the requirements reference; cite the requirement ID (e.g. `FR-RUB-03`) in the
  test that covers it. Where the SRS and the code disagree, the code wins and the SRS is the bug.
