"""The jail. Every path and every shell command an agent asks for comes through here.

SEC-01 (path jail), SEC-02 (bash deny-list), SEC-06 (secret hygiene), SEC-07 (egress),
SEC-09 (no autonomous publication), FR-CFG-06 (credentials file mode), FR-TOOL-07.

Two rules shape everything below:

* **Resolve, then compare.** `Path.resolve()` collapses `..` *and* follows symlinks, so the
  check happens on the real target, never on the string the model wrote.
* **A deny-list that blocks the build loop is worse than none** — it is a false sense of one
  plus a broken product. So the shell rules are targeted at the named patterns rather than at
  everything that looks vaguely dangerous, and `tests/test_security.py` carries as many
  allow cases as deny ones.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Iterable, Mapping
from pathlib import Path

# --------------------------------------------------------------------------- errors


class SecurityError(Exception):
    """Base for everything the guard refuses."""


class JailEscape(SecurityError):
    """A path that resolves outside the workspace."""


class CommandDenied(SecurityError):
    """A shell command matching the deny-list."""


# --------------------------------------------------------------------------- SEC-01, paths


def resolve_in_jail(root: Path | str, candidate: Path | str) -> Path:
    """The real, absolute path of `candidate`, or `JailEscape`.

    `candidate` may be relative (to `root`) or absolute. It need not exist: `resolve()` is
    non-strict, so a not-yet-written file still has its *parent* symlink-resolved and checked,
    which is the case that matters for `write_file`.
    """
    text = str(candidate)
    if "\x00" in text or "\x00" in str(root):
        raise JailEscape("path contains a null byte")

    jail = Path(root).expanduser().resolve()
    raw = Path(text).expanduser()
    resolved = (raw if raw.is_absolute() else jail / raw).resolve()

    if not resolved.is_relative_to(jail):
        raise JailEscape(f"{text!r} resolves to {resolved}, outside the workspace {jail}")
    return resolved


def is_in_jail(root: Path | str, candidate: Path | str) -> bool:
    """Predicate form, for callers that want to branch rather than catch."""
    try:
        resolve_in_jail(root, candidate)
    except JailEscape:
        return False
    return True


# --------------------------------------------------------------------------- SEC-02, the shell

#: Command substitution and process substitution. Denied outright rather than analysed: deciding
#: whether `$(...)` "reaches outside the jail" is undecidable, and a build has no honest need for
#: it. ponytail: blanket deny. If a real fixture needs `$(...)`, allow-list that one command.
SUBSTITUTION = ("$(", "`", "<(", ">(")

#: General-purpose network reach. Package managers are deliberately *not* here — a build cannot
#: install its dependencies without them (SEC-07 is a boundary, not an airgap; the real backstop
#: is SEC-08's per-turn snapshot).
NETWORK_COMMANDS = frozenset(
    {
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "ftp",
        "socat",
        "aria2c",
        "http",
        "httpie",
        "gh",
        "hub",
        "mail",
        "sendmail",
        "mailx",
    }
)

#: Commands whose non-flag arguments are paths, and so must each be inside the jail. Anything
#: not on this list gets the sensitive-directory scan only — otherwise
#: `git commit -m "fix the /etc/hosts parser"` is denied for containing a path-shaped word.
PATH_COMMANDS = frozenset(
    {
        "rm",
        "mv",
        "cp",
        "ln",
        "cat",
        "head",
        "tail",
        "touch",
        "mkdir",
        "rmdir",
        "chmod",
        "chown",
        "chgrp",
        "tee",
        "dd",
        "sed",
        "grep",
        "install",
        "truncate",
        "shred",
        "ls",
        "find",
        "stat",
        "file",
        "cd",
        "pushd",
        "source",
        ".",
        "open",
        "xdg-open",
    }
)

#: Shells whose `-c` argument is another command, and must be checked as one. Without this the
#: whole deny-list is one `sh -c '...'` away from irrelevant.
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})

#: `<installer> ... <flag>` pairs that install outside the project.
GLOBAL_FLAGS = frozenset({"-g", "--global", "--user", "global"})
NODE_INSTALLERS = frozenset({"npm", "pnpm", "yarn", "bun"})
SYSTEM_INSTALLERS = frozenset(
    {"brew", "apt", "apt-get", "yum", "dnf", "pacman", "apk", "zypper", "port", "snap"}
)
#: Installers that are global by construction, whatever flags follow.
ALWAYS_GLOBAL = {
    ("gem", "install"),
    ("cargo", "install"),
    ("go", "install"),
    ("pipx", "install"),
    ("uv", "tool"),
    ("npm", "link"),
}

#: Home-relative locations that hold credentials. Checked as resolved *paths*, not as substrings,
#: so a project's own `.config/` inside the workspace is untouched.
SENSITIVE_HOME_ENTRIES = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".config",
    ".docker",
    ".kube",
    ".loom",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
)

#: fd duplication (`2>&1`), not a file redirect.
_FD_DUP = re.compile(r"^&?\d*[<>]&$")
_REDIRECT = re.compile(r"^&?\d*(?:>>|>|<)$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_OPERATORS = frozenset({";", "&&", "||", "|", "&", "\n", "|&"})


def _sensitive_roots() -> tuple[Path, ...]:
    home = Path.home()
    return tuple(home / name for name in SENSITIVE_HOME_ENTRIES)


def _resolve_for_check(root: Path, token: str) -> Path | None:
    """Best-effort real path of a token. `None` when it cannot be one."""
    if "\x00" in token:
        return None
    try:
        raw = Path(token).expanduser()
        return (raw if raw.is_absolute() else Path(root) / raw).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _deny_if_sensitive(root: Path, token: str) -> None:
    resolved = _resolve_for_check(root, token)
    if resolved is None:
        return
    for sensitive in _sensitive_roots():
        if resolved == sensitive or resolved.is_relative_to(sensitive):
            raise CommandDenied(
                f"denied: {token!r} is a sensitive path outside the workspace ({sensitive})"
            )


def _deny_if_outside(root: Path, token: str, *, what: str) -> None:
    _deny_if_sensitive(root, token)
    try:
        resolve_in_jail(root, token)
    except JailEscape as exc:
        raise CommandDenied(f"denied: {what} {token!r} is outside the workspace") from exc


def _tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError as exc:
        raise CommandDenied(f"denied: the command could not be parsed ({exc})") from exc


def _segments(tokens: list[str]) -> list[list[str]]:
    out: list[list[str]] = [[]]
    for token in tokens:
        if token in _OPERATORS:
            out.append([])
        else:
            out[-1].append(token)
    return [seg for seg in out if seg]


def check_command(command: str, *, root: Path | str, _depth: int = 0) -> None:
    """Raise `CommandDenied` if `command` matches the deny-list. Otherwise return.

    ponytail: this reads the command, not the program it starts. `python -c "<anything>"` and a
    script the agent wrote itself are both opaque to it — SEC-08's per-turn git snapshot is the
    backstop for what gets through, and `run_bash` is the only caller.
    """
    if "\x00" in command:
        raise CommandDenied("denied: the command contains a null byte")
    if not command.strip():
        raise CommandDenied("denied: the command is empty")
    if _depth > 3:
        raise CommandDenied("denied: shell nesting is too deep to check")
    for marker in SUBSTITUTION:
        if marker in command:
            raise CommandDenied(f"denied: command substitution ({marker}) is not allowed")

    root = Path(root)
    for segment in _segments(_tokenize(command)):
        _check_segment(segment, root=root, depth=_depth)


def _check_segment(tokens: list[str], *, root: Path, depth: int) -> None:
    args = list(tokens)
    while args and _ASSIGNMENT.match(args[0]):  # FOO=bar cmd ...
        args.pop(0)
    if not args:
        return

    program = Path(args[0]).name
    rest = args[1:]
    words = [a for a in rest if not a.startswith("-")]

    if program in {"sudo", "su", "doas", "pkexec"}:
        raise CommandDenied(f"denied: sudo/privilege escalation ({tokens[0]})")
    if program in NETWORK_COMMANDS:
        raise CommandDenied(f"denied: {program} reaches the network (SEC-07/SEC-09)")
    if program == "git" and words and words[0] in {"push", "request-pull", "send-email"}:
        raise CommandDenied(f"denied: git push and friends are never allowed (SEC-09): {program}")
    _check_installers(program, rest, words)

    # `sh -c '<command>'` is another command wearing a hat. Check it as one, or the deny-list is
    # a formality.
    if program in SHELLS and "-c" in rest:
        index = rest.index("-c")
        if index + 1 < len(rest):
            check_command(rest[index + 1], root=root, _depth=depth + 1)
        return

    _check_redirects(rest, root=root)

    if program in PATH_COMMANDS:
        if program in {"cd", "pushd"} and not words:
            raise CommandDenied("denied: bare `cd` leaves the workspace for $HOME")
        for arg in words:
            if "$" in arg:
                raise CommandDenied(
                    f"denied: variable expansion in a path argument ({arg!r}) cannot be checked"
                )
            _deny_if_outside(root, arg, what="path")
    else:
        for arg in words:
            _deny_if_sensitive(root, arg)


def _check_installers(program: str, rest: list[str], words: list[str]) -> None:
    sub = words[0] if words else ""
    if (program, sub) in ALWAYS_GLOBAL:
        raise CommandDenied(f"denied: global package install ({program} {sub})")
    if program in SYSTEM_INSTALLERS and sub in {"install", "add", "upgrade", "tap", "reinstall"}:
        raise CommandDenied(f"denied: global package install ({program} {sub})")
    if program in NODE_INSTALLERS and set(rest) & GLOBAL_FLAGS:
        raise CommandDenied(f"denied: global package install ({program} with a global flag)")
    if program.startswith("pip") and "--user" in rest:
        raise CommandDenied(
            "denied: global package install (pip --user writes outside the project)"
        )


def _check_redirects(rest: list[str], *, root: Path) -> None:
    index = 0
    while index < len(rest):
        token = rest[index]
        if _FD_DUP.match(token):  # 2>&1 — a descriptor, not a file
            index += 2
            continue
        if _REDIRECT.match(token):
            if index + 1 < len(rest):
                _deny_if_outside(root, rest[index + 1], what="redirect target")
            index += 2
            continue
        index += 1


# --------------------------------------------------------------------------- SEC-06, secrets

#: An environment variable whose *name* looks like a credential. Matching on the name and
#: redacting the value beats matching "key-shaped strings" in the output, which misses things.
SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE)

#: Below this a "secret" is more likely to be a flag or a word, and redacting it would corrupt
#: every line it appears in.
MIN_SECRET_LENGTH = 8

REDACTED = "[redacted]"


def secret_values(env: Mapping[str, str] | None = None) -> set[str]:
    """Every environment value worth keeping out of a transcript."""
    env = os.environ if env is None else env
    return {
        value
        for name, value in env.items()
        if SECRET_NAME.search(name) and len(value) >= MIN_SECRET_LENGTH
    }


def redact(
    text: str,
    *,
    env: Mapping[str, str] | None = None,
    extra: Iterable[str] = (),
) -> str:
    """Replace every known secret with `[redacted]` (SEC-06, NFR-OBS-03).

    Applied to tool output before it reaches the model, an event or an artifact — `env` in a
    build's shell is the shortest path from a user's API key into a transcript.
    """
    values = secret_values(env) | {v for v in extra if len(v) >= MIN_SECRET_LENGTH}
    for value in sorted(values, key=len, reverse=True):  # longest first: overlaps stay covered
        text = text.replace(value, REDACTED)
    return text


# --------------------------------------------------------------------------- FR-CFG-06


def read_credentials(path: Path) -> dict[str, str]:
    """Provider keys from `~/.loom/credentials.json`, or `{}` if there is no such file.

    A group- or world-readable credentials file is refused rather than read: on a shared machine
    it is already leaked, and silently using it teaches the user that it was fine.
    """
    path = Path(path)
    if not path.is_file():
        return {}
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(
            f"{path} is mode {mode:04o}; credentials must be 0600. Run: chmod 600 {path}"
        )
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path} is not valid JSON, so credentials could not be read: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise ValueError(
            f"{path} must hold a JSON object of credentials, not {type(loaded).__name__}"
        )
    return {str(k): str(v) for k, v in loaded.items()}
