"""The git wrapper. FR-WS-01/02/03, SEC-08.

SEC-08 says everything the agent does happens inside a git repository with a snapshot per turn,
so any change is revertible. This file is the whole of that claim: if it is wrong, every other
security control is a suggestion.

Three things git does *not* do that this module has to:

* `git reset --hard` leaves untracked files behind. "Restore the exact tree" means the reset
  plus a `clean`, or turn 6's half-written module survives into turn 3.
* `git clean -fd` without `-x` deliberately spares ignored files, which is what keeps `.loom/`
  — the event log of the very run doing the resetting — from being deleted underneath it.
* A repo with no commits makes every operation a special case, so `create()`/`open()` plant an
  empty baseline commit and nothing below ever branches on it.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Loom commits under its own identity rather than the user's. These are machine snapshots, and
#: attributing generated code to whoever happened to be logged in is a small lie that shows up
#: in `git blame` forever. The `-c` form never writes to the user's git config.
GIT_CONFIG = {"user.name": "Loom", "user.email": "loom@localhost", "commit.gpgsign": "false"}
IDENTITY = tuple(arg for kv in GIT_CONFIG.items() for arg in ("-c", "=".join(kv)))

#: Snapshot labels become ref names, so they are validated before they reach git rather than
#: after: `refs/loom/snap/../../heads/main` is a sentence git would happily parse.
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

SNAP_REF = "refs/loom/snap"
BASE_REF = "refs/loom/base"

#: Loom's own state is never the user's to commit and never Loom's to revert. Enforced with a
#: pathspec here rather than by trusting the `.gitignore` that FR-WS-04 writes, so this module
#: is correct on its own: without it, a project whose `.loom/` is not ignored would have its
#: event log committed into snapshots and then *rolled back* by the reset that reads it.
NOT_LOOM = ("--", ".", ":!.loom")


class WorkspaceError(RuntimeError):
    """Anything git refused, or that Loom refuses to ask git for."""


class DirtyWorkspace(WorkspaceError):
    """FR-WS-02 — there is uncommitted work here and it is not Loom's."""


@dataclass(frozen=True)
class Snapshot:
    """One turn's revert point. `label` is durable; `sha` is what it resolved to."""

    label: str
    sha: str


class Workspace:
    """A git repository Loom is allowed to write in.

    Synchronous on purpose. git is fast, snapshots happen once per turn, and an async wrapper
    around four `subprocess.run` calls would buy nothing but a colour.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    # ----------------------------------------------------------------- construction

    @classmethod
    def create(cls, root: Path) -> Workspace:
        """Make the directory a repo if it is not one, and give it a baseline commit."""
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        workspace = cls(root)
        if not workspace.is_repo():
            workspace._git("init", "--quiet")
        workspace._ensure_baseline()
        return workspace

    @classmethod
    def open(cls, root: Path) -> Workspace:
        """An existing repo with nothing uncommitted in it. FR-WS-02."""
        workspace = cls(root)
        if not workspace.is_repo():
            raise WorkspaceError(
                f"{workspace.root} is not a git repository. Run `loom init -C {workspace.root}`."
            )
        workspace.require_clean()
        workspace._ensure_baseline()
        return workspace

    def is_repo(self) -> bool:
        return self._git("rev-parse", "--git-dir", check=False) is not None

    def _ensure_baseline(self) -> None:
        if self.head() is None:
            self._commit("loom: baseline")

    # ----------------------------------------------------------------- state

    def head(self) -> str | None:
        """The current commit, or `None` in a repo that has none yet."""
        return self._git("rev-parse", "HEAD", check=False)

    def status(self) -> list[str]:
        """Porcelain lines. Ignored files are absent, which is why `.loom/` never shows up."""
        out = self._require("status", "--porcelain", *NOT_LOOM)
        return [line for line in out.splitlines() if line.strip()]

    def is_dirty(self) -> bool:
        return bool(self.status())

    def require_clean(self) -> None:
        """FR-WS-02 — refused with the file list, never silently committed."""
        changes = self.status()
        if not changes:
            return
        shown = "\n  ".join(changes[:8])
        more = f"\n  …and {len(changes) - 8} more" if len(changes) > 8 else ""
        raise DirtyWorkspace(
            f"{self.root} has uncommitted changes:\n  {shown}{more}\n"
            "Loom will not commit work it did not write. Commit or stash it first."
        )

    def tree_hash(self) -> str:
        """Content hash of the working tree, including uncommitted work.

        WP-4.2 memoizes graded criteria on this. Staging is a side effect: `write-tree` reads
        the index, so the index has to be current. Everything here gets committed or reset
        anyway, so there is nothing for it to disturb.
        """
        self._require("add", "-A", *NOT_LOOM)
        return self._require("write-tree")

    # ----------------------------------------------------------------- FR-WS-01

    def snapshot(self, label: str) -> Snapshot:
        """One revert point, per turn. Empty turns get one too — otherwise "reset to turn 5"
        quietly means turn 3 on any turn where the model only read files."""
        self._check_label(label)
        self._require("add", "-A", *NOT_LOOM)
        self._commit(f"loom: {label}")
        sha = self.head()
        assert sha is not None  # _commit just made one
        self._require("update-ref", f"{SNAP_REF}/{label}", sha)
        return Snapshot(label, sha)

    def resolve(self, label: str) -> str:
        """A label, a `base/<phase>` marker, or any revision git understands."""
        for candidate in (f"{SNAP_REF}/{label}", f"refs/loom/{label}", label):
            sha = self._git(
                "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}", check=False
            )
            if sha:
                return sha
        raise WorkspaceError(f"no snapshot or revision named {label!r} in {self.root}")

    def reset_to(self, label: str) -> str:
        """Restore the exact tree of `label` — modifications, deletions and additions alike."""
        sha = self.resolve(label)
        self._require("reset", "--hard", "--quiet", sha)
        # No `-x`: ignored files survive on purpose — virtualenvs are expensive to rebuild for
        # no reason. `.loom/` is excluded explicitly as well, because it holds the event log of
        # the very run doing the resetting, ignored or not.
        self._require("clean", "-fdq", "-e", ".loom")
        return sha

    def begin_phase(self, phase: str) -> str:
        """Mark where a phase started, so its turn commits can be collapsed on success.

        Kept as a ref rather than in memory: a resumed process still has to be able to finish
        the phase it interrupted.
        """
        self._check_label(phase)
        sha = self.head()
        assert sha is not None  # every constructor plants a baseline
        self._require("update-ref", f"{BASE_REF}/{phase}", sha)
        return sha

    def commit_phase(self, phase: str, message: str | None = None) -> str:
        """Collapse this phase's turn snapshots into one commit. FR-WS-01.

        The turn refs are left in place: their commits leave the branch history but stay
        reachable, so `reset_to("turn-3")` still works after a squash.
        """
        self._check_label(phase)
        base = self._git("rev-parse", "--verify", "--quiet", f"{BASE_REF}/{phase}", check=False)
        if not base:
            raise WorkspaceError(
                f"no phase base for {phase!r} — call begin_phase({phase!r}) before commit_phase"
            )
        # FR-WS-03: rewrite only what Loom itself laid down. If HEAD has moved somewhere the
        # base does not lead to, a soft reset would silently delete whatever is in between.
        if self._git("merge-base", "--is-ancestor", base, "HEAD", check=False) is None:
            raise WorkspaceError(
                f"the {phase!r} phase base {base[:8]} is not an ancestor of HEAD; refusing to "
                "rewrite history Loom did not create"
            )
        self._require("reset", "--soft", base)
        self._commit(message or f"loom({phase}): {phase} phase")
        sha = self.head()
        assert sha is not None
        return sha

    # ----------------------------------------------------------------- plumbing

    def _check_label(self, label: str) -> None:
        if not LABEL.fullmatch(label or ""):
            raise WorkspaceError(
                f"invalid snapshot label {label!r}: letters, digits, dot, dash and underscore "
                "only, up to 64 characters"
            )

    def _commit(self, message: str) -> None:
        self._require(*IDENTITY, "commit", "--allow-empty", "--quiet", "-m", message)

    def _require(self, *args: str) -> str:
        out = self._git(*args)
        if out is None:  # pragma: no cover - _git raises first when check is on
            raise WorkspaceError(f"git {' '.join(args)} produced no output")
        return out

    def _git(self, *args: str, check: bool = True) -> str | None:
        """Run git. `None` on failure when `check` is off, `WorkspaceError` when it is on.

        Argument list, never a shell string: nothing here is ever interpolated into a command
        line, so a label or phase name cannot become a second command.
        """
        result = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)
        if result.returncode != 0:
            if not check:
                return None
            raise WorkspaceError(
                f"git {' '.join(args)} failed in {self.root}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout.strip()
