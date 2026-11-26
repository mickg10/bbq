"""Shared fixtures: the fake CyberQ on an ephemeral port.

The fake (dev.fake_cyberq) runs as a real uvicorn server in a thread, so
the device client's httpx traffic is exercised exactly as it would be
against 192.168.17.6 — including the request log the allowlist tests
assert on.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterator

import httpx
import pytest
import uvicorn

from dev import fake_cyberq


@pytest.fixture()
def fake_device() -> Iterator[str]:
    """Start the fake CyberQ on an ephemeral port; yield its base URL."""
    fake_cyberq.reset()
    config = uvicorn.Config(
        fake_cyberq.app, host="127.0.0.1", port=0, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "fake CyberQ did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


@pytest.fixture()
def device_log(fake_device: str) -> Callable[[], list[dict]]:
    """Fetch the fake's request log (/_log entries excluded from assertions
    by the tests themselves — this helper's own requests land there)."""

    def fetch() -> list[dict]:
        return httpx.get(f"{fake_device}/_log", timeout=5).json()

    return fetch
