# Loom

CLI that takes a founder's idea and produces a working, tested codebase.
Pipeline: Validate -> Plan -> Design -> Build+Test. Terminal only. No web UI, no server.

## Invariants — do not violate without asking
- Python 3.11+. No Node dependency, ever.
- `loom/` core must never import `typer`, `rich`, or `questionary`. CLI concerns live in
  `loom/cli.py` and `loom/gates.py` only. The core must be drivable from a library.
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
- No `print()` in `loom/` — structured events via `session.log_event`.
- Tests use `FakeLLM` (loom/testing/fake_llm.py). No network in unit tests.

## Cost rules
- The judge model for rubric grading is pinned cheap in config and its spend is ledgered
  under phase "judge", separately from the phase that triggered it.
- Shell criteria grade before judge criteria. A failed hard_fail shell criterion returns
  immediately without spending a single judge call.

## Layout
loom/{cli,config,contracts,session,gates,ledger,scaffold,security,workspace,rubric,cache}.py
loom/agent/{loop,context,providers}.py  loom/agent/tools/{fs,bash,web,ask_user,registry}.py
loom/phases/{base,validate,plan,design,build}.py
loom/testing/fake_llm.py  loom/prompts/*.md  loom/blueprints/*/

## Working agreement
- Stay inside the file scope given in the task. Ask before touching anything else.
- Write the test first, then the implementation.
- Append one line to docs/DECISIONS.md for any non-obvious choice.
