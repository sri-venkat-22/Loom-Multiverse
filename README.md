# Loom

[![CI](https://github.com/sri-venkat-22/Loom-Multiverse/actions/workflows/check.yml/badge.svg)](https://github.com/sri-venkat-22/Loom-Multiverse/actions/workflows/check.yml)
[![PyPI](https://img.shields.io/pypi/v/loom-cli.svg)](https://pypi.org/project/loom-cli/)
[![Python](https://img.shields.io/pypi/pyversions/loom-cli.svg)](https://pypi.org/project/loom-cli/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Loom takes a founder's idea and produces a working, tested codebase.** You describe the thing in
a sentence; Loom researches it, writes a PRD, picks a stack, then scaffolds and edits the code in a
git repo — grading itself against a rubric until the build passes.

```
Validate ──▶ Plan ──▶ Design ──▶ Build + Test
  market      PRD      stack &      scaffold, write,
  & risks              rubric       test until it passes
```

Each phase produces one JSON artifact and stops at a **gate** you approve, reject with feedback, or
edit by hand. It runs entirely in your terminal, calls any model through
[LiteLLM](https://github.com/BerriAI/litellm), and never pushes to a remote or touches uncommitted
work it did not write.

## Demo

[![asciicast](https://asciinema.org/a/000000.svg)](https://asciinema.org/a/000000)

<!-- The badge above activates once the cast is uploaded. Record and upload it with:
       scripts/record-demo.sh          # writes docs/loom-demo.cast
       asciinema upload docs/loom-demo.cast
     then replace 000000 with the returned cast id. Until then, play it locally:
       asciinema play docs/loom-demo.cast   -->

A session looks like this:

```console
$ loom run "a URL shortener with click analytics"
◆ validate   researching market, competitors and risks … ✓  → validation.json
  gate: approve / reject with feedback / edit  ▸ approve
◆ plan       turning the validation into a PRD … ✓         → prd.json          ▸ approve
◆ design     choosing stack, file manifest and rubric … ✓ → design.json       ▸ approve
◆ build      scaffold → write → test, grading each round
    turn  6  · pytest 4/7 · $0.02
    turn 11  · pytest 7/7 · rubric 0.91 ≥ 0.85 ✓
✓ build passed — 11 turns, $0.04, squashed into one commit
```

## Install

```bash
pipx install loom-cli
```

Then `loom` (no arguments) opens an interactive session, or drive it a phase at a time:

```bash
loom run "a URL shortener with click analytics"
```

Requires Python 3.11+. No Node, ever.

## Setup

Loom is bring-your-own-key. Set the key for whichever tier you point it at — the default build
model is the free NVIDIA NIM tier:

```bash
export NVIDIA_NIM_API_KEY=...   # cheap/default build + judge model
export ANTHROPIC_API_KEY=...    # the strong tier (claude-sonnet-5)
export TAVILY_API_KEY=...       # web search, used by the validate phase
```

Keys are read from the environment, then a git-ignored `.env` (copy `.env.example`), then
`~/.loom/credentials.json` — in that order. A key is never written into the workspace and never
appears in any output. See `.env.example` for the full list.

## How it works

```bash
loom init                      # create .loom/ and make sure this is a git repo
loom run "<idea>"              # the whole pipeline, gate between each phase
loom run "<idea>" --yes        # no gates — full run with no TTY (CI-friendly)

loom validate "<idea>"         # one phase at a time …
loom plan  ·  loom design  ·  loom build

loom resume                    # continue the last run from its last completed phase
loom replay design             # re-run one phase against cached inputs, print the diff
loom status                    # config in effect, latest run, spend to date
loom cost                      # spend by phase and by model
loom preview                   # boot the built app and print a local URL
loom doctor  ·  loom bug       # environment checks · redacted diagnostic bundle
```

Useful flags on `run`: `--from/--to <phase>`, `--model`, `--effort {fast,balanced,thorough}`,
`--budget <usd>`, `--blueprint <path>`, `--no-cache`, `--json`.

Inside the interactive session, `/` opens a command palette (`/help` lists everything), `@`
completes workspace paths, `!` runs one guarded shell command, and `#` records a standing
constraint. `/background` runs a phase without blocking the prompt; `/rewind` restores the
workspace and history to an earlier turn from the per-turn git snapshots.

### What it does with your repo

Every agent turn is a git snapshot, so any change is revertible and a failed build never leaves a
mess. Loom refuses to start over uncommitted work it did not write, never force-anything, and never
`git push`. The research phases (validate/plan/design) get **no** filesystem or shell access — a
security boundary, because they ingest untrusted web content.

## Development

```bash
uv sync                # install
make check             # lint + types + tests — the same gate CI runs
make fast              # the on-save tier: no real git, no build loops (< 10s)
make test              # unit + cassette tests
uv run pytest -m live  # tests that hit real provider APIs (cost money)
```

Contracts are Pydantic v2; the terminal layer (`loom/cli.py`, `loom/tui/`) is kept strictly
separate from the provider-agnostic core, which imports no terminal library and stays drivable as a
library with no TTY. See [CLAUDE.md](CLAUDE.md) for the invariants and [docs/SRS.md](docs/SRS.md)
for the requirements.

## License

[Apache-2.0](LICENSE).
