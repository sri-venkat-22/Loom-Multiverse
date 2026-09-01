"""WP-6.4 / FR-BP-03 — `loom preview` boots the built app and never leaks a port.

The socket block in conftest refuses outbound *connects*, so the real-process test proves
teardown by binding (listen), never by connecting: while the child holds the port a bind fails,
and after `_terminate` the same bind succeeds. That is exactly "never leave a port bound".
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

from loom.preview import (
    PreviewError,
    _asgi_attr,
    _await_health,
    _find_asgi,
    _free_port,
    _responds,
    _terminate,
)


def _bind_fails(port: int) -> bool:
    """True if 127.0.0.1:port is already held. A bind, not a connect — allowed by conftest."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


# ------------------------------------------------------------------ target detection


@pytest.mark.parametrize(
    "source, expected",
    [
        ("app = FastAPI(title='x')", "app"),
        ("from fastapi import FastAPI\napi = FastAPI()\n", "api"),
        ("srv=FastAPI( )", "srv"),
        ("# app = FastAPI()  (a comment)\nreal = FastAPI()", "real"),
        ("import fastapi\nx = 1\n", None),
        ("app = Flask(__name__)", None),
    ],
)
def test_asgi_attr(source: str, expected: str | None) -> None:
    assert _asgi_attr(source) == expected


def test_find_asgi_prefers_app_main(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("app = FastAPI()\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("app = FastAPI()\n", encoding="utf-8")
    assert _find_asgi(tmp_path) == "app.main:app"


def test_find_asgi_maps_flat_module(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("api = FastAPI()\n", encoding="utf-8")
    assert _find_asgi(tmp_path) == "main:api"


def test_find_asgi_raises_for_non_web_project(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def main():\n    print('hi')\n", encoding="utf-8")
    with pytest.raises(PreviewError, match="no web app to preview"):
        _find_asgi(tmp_path)


# ------------------------------------------------------------------ health branch logic


def test_responds_true_on_any_http_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_404(*_a: object, **_k: object) -> object:
        raise urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", raise_404)
    assert _responds("http://127.0.0.1:1") is True  # a 404 still proves the ASGI app answered


def test_responds_false_when_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> object:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    assert _responds("http://127.0.0.1:1") is False


def test_await_health_reports_early_exit(tmp_path: Path) -> None:
    log = tmp_path / "preview.log"
    log.write_text("boom: could not import app\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    proc.wait()
    with pytest.raises(PreviewError, match="exited before it served") as caught:
        _await_health("http://127.0.0.1:1", proc, log, timeout=2.0)
    assert "boom" in str(caught.value)  # the uvicorn log tail is surfaced, not swallowed


# ------------------------------------------------------------------ the MUST: no leaked port


def test_terminate_releases_the_port(tmp_path: Path) -> None:
    port = _free_port("127.0.0.1")
    log = open(tmp_path / "srv.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not _bind_fails(port):
            if proc.poll() is not None:
                raise AssertionError("http.server exited before binding")
            time.sleep(0.05)
        assert _bind_fails(port), "server never claimed the port"
    finally:
        _terminate(proc)
        log.close()

    assert proc.poll() is not None  # FR-BP-03: the process is gone
    assert not _bind_fails(port)  # FR-BP-03: the port is free again


def test_terminate_is_idempotent_on_a_dead_process() -> None:
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    _terminate(proc)  # must not raise on an already-dead child
    assert proc.poll() is not None
