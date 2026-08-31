"""Completion for the two things that complete in the prompt: `/commands` and `@paths`.

Both are `prompt_toolkit` `Completer`s, so `↑/↓` navigate the menu, `Tab` accepts, and `Esc`
closes — behaviour the framework already has (FR-SLASH-03). The only logic here is *what* to
offer:

* `SlashCompleter` offers the filtered command list, ranked name-before-description, from the one
  registry in `commands.py` (FR-SLASH-02/05). No second list.
* `PathCompleter` offers paths **inside the jail only**, and never `.git/` or `.loom/`
  (FR-REPL-08). It reuses `security.is_in_jail`, so a symlink out of the workspace is not
  offered here for the same reason the agent could not follow it.

Completers are pure over their `Document` — a test calls `get_completions` directly, no terminal.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from loom.security import is_in_jail
from loom.tui.commands import Command, filter_commands

#: The command token being typed: a leading `/` then non-space, and nothing before it. Once a
#: space is typed the palette is closed and the rest is arguments.
_SLASH = re.compile(r"^/(\S*)$")

#: Directories a path completion never descends into (FR-REPL-08). Loom's own state and git's.
HIDDEN_DIRS = frozenset({".git", ".loom"})


class SlashCompleter(Completer):
    """The `/` palette as a completion menu, filtered through the shared registry."""

    def get_completions(self, document: Document, complete_event: object) -> Iterable[Completion]:
        text = document.text_before_cursor
        match = _SLASH.match(text)
        if match is None:
            return
        query = match.group(1)
        for command in filter_commands(query):
            yield Completion(
                text=f"/{command.name}",
                start_position=-len(text),
                display=f"/{command.name}",
                display_meta=command.summary,
            )


class PathCompleter(Completer):
    """`@`-triggered path completion, rooted at the workspace and confined to it."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def get_completions(self, document: Document, complete_event: object) -> Iterable[Completion]:
        text = document.text_before_cursor
        at = text.rfind("@")
        if at == -1:
            return
        fragment = text[at + 1 :]
        if any(c.isspace() for c in fragment):  # the @-reference ended at a space
            return
        yield from self._paths(fragment)

    def _paths(self, fragment: str) -> Iterable[Completion]:
        # Split the fragment into an already-typed directory and the partial leaf being matched.
        prefix_dir, _, leaf = fragment.rpartition("/")
        base = self.root / prefix_dir if prefix_dir else self.root
        if not base.is_dir() or not is_in_jail(self.root, base):
            return
        for entry in sorted(base.iterdir(), key=lambda p: p.name):
            if entry.name.startswith("."):
                if entry.name in HIDDEN_DIRS or not leaf.startswith("."):
                    # Hidden entries only surface when the user is explicitly typing a dot; the
                    # jail's own `.git`/`.loom` never surface at all.
                    continue
            if not entry.name.startswith(leaf):
                continue
            if not is_in_jail(self.root, entry):  # a symlink pointing outside — never offer it
                continue
            suffix = "/" if entry.is_dir() else ""
            yield Completion(
                text=entry.name + suffix,
                start_position=-len(leaf),
                display=entry.name + suffix,
            )


def command_completer() -> SlashCompleter:
    return SlashCompleter()


def path_completer(root: Path) -> PathCompleter:
    return PathCompleter(root)


__all__ = ["SlashCompleter", "PathCompleter", "command_completer", "path_completer", "Command"]
