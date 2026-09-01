"""`loom preview` — boot the built app on a free port, prove it answers, tear it down. WP-6.4.

FR-BP-03: boot the built application, print a local URL, and **terminate the process on exit,
never leaving a port bound**. That last clause is the whole risk here, so the teardown kills the
child's process *group* (uvicorn spawns workers) and the CLI drives this through a context manager
whose `finally` runs on Ctrl-C, on a boot failure, and on the app dying under it.

Presentation stays in `loom/cli.py`: this module prints nothing and returns a `Preview` describing
what it booted. POSIX only — Loom's toolchain (`uv`, `uvicorn`, `make`) is, and process-group
signalling has no portable Windows equivalent worth carrying for R1.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

#: Where a FastAPI instance tends to live, most-specific first. The blueprint writes app/main.py;
#: the other two are what a flat, hand- or model-written app usually calls it.
_CANDIDATES = ("app/main.py", "main.py", "app.py")

#: `<name> = FastAPI(` on its own logical line. Matched against source text, never by importing:
#: importing generated code to introspect it runs its side effects, which a preview must not.
_ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*FastAPI\s*\(", re.MULTILINE)


class PreviewError(RuntimeError):
    """Nothing here is previewable, or the app never came up."""


@dataclass(frozen=True)
class Preview:
    """A running preview. `target` is the uvicorn app path, e.g. ``app.main:app``."""

    url: str
    port: int
    target: str
    log: Path


def _asgi_attr(source: str) -> str | None:
    match = _ASSIGN.search(source)
    return match.group(1) if match else None


def _find_asgi(root: Path) -> str:
    """The uvicorn ``module:attr`` for the app in `root`, or `PreviewError` if there is none."""
    for rel in _CANDIDATES:
        path = root / rel
        if not path.is_file():
            continue
        attr = _asgi_attr(path.read_text(encoding="utf-8", errors="replace"))
        if attr:
            module = rel[:-3].replace("/", ".")  # app/main.py -> app.main
            return f"{module}:{attr}"
    raise PreviewError(
        "no web app to preview here — looked for a FastAPI app in "
        + ", ".join(_CANDIDATES)
        + ". `loom preview` boots HTTP apps; a CLI project has nothing to serve."
    )


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, 0))
        return int(probe.getsockname()[1])
    # ponytail: a tiny window between this close and uvicorn's bind; fine for a local preview.


def _responds(url: str) -> bool:
    """True the moment the app returns *any* HTTP status — a 404 root still proves it serves."""
    try:
        with urllib.request.urlopen(url, timeout=1):  # noqa: S310 - fixed localhost URL
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError):
        return False


def _tail(log: Path, lines: int = 15) -> str:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = "\n".join(text.splitlines()[-lines:]).strip()
    return f"\n--- uvicorn output ---\n{tail}" if tail else ""


def _await_health(url: str, proc: subprocess.Popen[bytes], log: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise PreviewError(
                f"the app exited before it served a request (uvicorn exit {proc.returncode})."
                + _tail(log)
            )
        if _responds(url):
            return
        time.sleep(0.2)
    raise PreviewError(f"the app did not answer within {timeout:.0f}s." + _tail(log))


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """Kill the app and everything it spawned. Idempotent, and safe on an already-dead child."""
    if proc.poll() is not None:
        return
    # The whole group, not just uvicorn: --reload and workers are children still holding the port,
    # and a bare terminate() would orphan them. This is the "never leave a port bound" guarantee.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


@contextlib.contextmanager
def preview(root: Path, *, host: str = "127.0.0.1", timeout: float = 30.0) -> Iterator[Preview]:
    """Boot the built app under `root`, wait until it answers, and always tear it down on exit."""
    root = Path(root).resolve()
    target = _find_asgi(root)
    port = _free_port(host)
    url = f"http://{host}:{port}"
    log_path = root / ".loom" / "preview.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with open(log_path, "w", encoding="utf-8") as log:
        try:
            proc = subprocess.Popen(
                ["uv", "run", "uvicorn", target, "--host", host, "--port", str(port)],
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group; _terminate reaches uvicorn's workers
            )
        except FileNotFoundError as exc:
            raise PreviewError("`uv` is not on PATH; install uv to preview the app.") from exc
        try:
            _await_health(url, proc, log_path, timeout)
            yield Preview(url=url, port=port, target=target, log=log_path)
        finally:
            _terminate(proc)
