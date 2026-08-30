"""NFR-TEST-02 — no unit test touches the network, enforced rather than trusted."""

from __future__ import annotations

import socket
from typing import Any, NoReturn

import pytest


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
