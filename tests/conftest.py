"""Shared fixtures.

The whole test suite runs against an in-process FastAPI app — no
sockets, no real USB. `client` uses FastAPI's `TestClient` (which
wraps `httpx`) so each test gets an isolated lifespan: the printer
registry loads from a tmp_path-backed `printers.json` and discovery
returns whatever the test stubs out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.constants import DEFAULT_API_KEY
from src.server import create_app


@pytest.fixture
def auth_headers() -> dict:
    """Send the real product handshake on every authenticated call —
    keeps tests honest about which routes are gated."""
    return {"X-Api-Key": DEFAULT_API_KEY}


@pytest.fixture
def tmp_registry(tmp_path: Path) -> Path:
    """Isolated `printers.json` per test so state doesn't bleed."""
    return tmp_path / "printers.json"


@pytest.fixture
def config(tmp_registry: Path) -> dict:
    return {
        "server": {
            "port": 9999,
            "registry_path": str(tmp_registry),
        },
    }


@pytest.fixture
def client(config: dict) -> Iterator[TestClient]:
    """Mount the app in-process with USB / LAN / BT discovery stubbed
    out — every test that needs discovered devices supplies them via
    `monkeypatch` on `src.devices.scan.discover_*`."""
    with patch("src.devices.scan.discover_usb", return_value=[]), \
         patch("src.devices.scan.discover_network", return_value=[]), \
         patch("src.devices.scan.discover_bluetooth", return_value=[]):
        app = create_app(config)
        with TestClient(app) as c:
            yield c
