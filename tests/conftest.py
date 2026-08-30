"""Test-tier plumbing.

NFR-TEST-02 — no unit test touches the network, enforced rather than trusted.
NFR-TEST-01 — three tiers, and the one that costs money never runs by accident.
"""

from __future__ import annotations

import socket
from typing import Any, NoReturn

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """`live` runs only when the `-m` expression names it.

    This is a guard, not a convenience. The obvious way to write it — `addopts = "-m 'not
    live'"` — is silently defeated by any command-line `-m`, because the last `-m` wins. So
    `pytest -m "not slow"`, the exact command for the fast tier, would have collected the live
    tests and spent real money. Skipping by keyword instead makes every marker expression
    composable and safe.
    """
    markexpr = str(config.getoption("markexpr", default="") or "")
    if "live" in markexpr:
        return
    skip = pytest.mark.skip(reason="live tier — run with `-m live` (real API calls, real money)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def block_the_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test except `@pytest.mark.live` gets a socket that refuses to connect.

    Listening and local IPC are untouched — only outbound connections fail, and they fail with
    a message naming the requirement rather than a timeout twenty seconds later.
    """
    if request.node.get_closest_marker("live"):
        return

    def refuse(*args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError(
            "a unit test tried to open a network connection (NFR-TEST-02). Use FakeLLM, a "
            "cassette, or mark the test @pytest.mark.live."
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
