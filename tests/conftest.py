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
def tmp_terminal_registry(tmp_path: Path) -> Path:
    """Isolated `terminals.json` per test."""
    return tmp_path / "terminals.json"


@pytest.fixture
def config(tmp_registry: Path, tmp_terminal_registry: Path, tmp_path: Path) -> dict:
    return {
        "server": {
            "port": 9999,
            "registry_path": str(tmp_registry),
            "terminal_registry_path": str(tmp_terminal_registry),
            # PET-1087 — the work journal and the device settings the app keeps
            # on the station. Per-test paths: the defaults are the install
            # folder, and a test has no business writing there.
            "journal_path": str(tmp_path / "work-journal.json"),
            "station_settings_path": str(tmp_path / "app-settings.json"),
            # Whether the client app offers offline mode — per test too.
            "offline_offer_path": str(tmp_path / "offline-offer.json"),
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
