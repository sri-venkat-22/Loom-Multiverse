#!/usr/bin/env bash
# Record the README's asciinema cast → docs/loom-demo.cast
#
#   scripts/record-demo.sh                       # a no-cost tour (help, init, status)
#   scripts/record-demo.sh 'loom run "an idea"'  # record a real build (needs keys + money)
#
# Then upload and wire it into the README:
#   asciinema upload docs/loom-demo.cast         # prints a cast id / URL
#   # replace the 000000 in README.md's asciinema badge with that id
#
# Needs asciinema (https://asciinema.org): `brew install asciinema` or `pipx install asciinema`.
set -euo pipefail

cd "$(dirname "$0")/.."
out="docs/loom-demo.cast"
mkdir -p docs

if ! command -v asciinema >/dev/null 2>&1; then
  echo "asciinema not found — install it: brew install asciinema (or pipx install asciinema)" >&2
  exit 1
fi

# ponytail: default to a demo that spends nothing. A real `loom run` is the better cast, but it
# needs provider keys and costs money — pass it as an argument when you want that instead.
cmd="${1:-loom --help && loom init && loom status}"

echo "recording: $cmd"
asciinema rec "$out" --overwrite --title "Loom" --command "bash -lc '$cmd'"
echo "wrote $out — play it with: asciinema play $out"
