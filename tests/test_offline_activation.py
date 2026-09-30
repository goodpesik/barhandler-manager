"""PET-972 — offline mode is switched on from Petshandler and kept here.

The online app takes a device token from the server and hands it to the
manager; the manager keeps the secrets with the operating system, the rest of
the config in a private file, and refuses to switch off (or to update) while
operations made offline have not reached the server.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.constants import DEFAULT_API_KEY
from src.server import create_app
from src.services import offline_secrets, offline_service, offline_state

KEY = {"X-Api-Key": DEFAULT_API_KEY}
PAYLOAD = {
    "appid": "bark-01",
    "deviceId": "till-7f3a",
    "deviceToken": "pho_secret_token_value",
    "apiBase": "https://api.petshandler.com/api",
    "shopName": "Барк",
    "expiresAt": "2027-09-30T00:00:00.000Z",
}


class FakeService:
    """The supervisor, reduced to what the routes and the busy guard use."""

    def __init__(self, queued):
        self.queued = queued
        self.stopped = 0
        self.state = offline_service.OfflineServiceState(running=True, extra={"queued": queued})

    async def run_forever(self):
        return None

    async def stop(self):
        return None

    async def stop_service(self):
        self.stopped += 1

    async def refresh(self):
        return None if self.queued is None else {"queued": self.queued}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(offline_state, "APP_DIR", tmp_path)
    monkeypatch.setattr(offline_secrets, "_backend", lambda: "file")
    monkeypatch.delenv("BHM_OFFLINE_CONFIG_FILE", raising=False)
    return tmp_path


def _client(config, monkeypatch, service):
    """The app with the given supervisor (None = a build without the runtime)."""
    monkeypatch.setattr(offline_service, "node_path", lambda root=None: Path(__file__) if service else Path("/no/node"))
    monkeypatch.setattr(offline_service, "OfflineService", lambda *a, **k: service)
    return TestClient(create_app(config))


@pytest.fixture
def client_for(config, monkeypatch, home):
    stack = []

    def make(service):
        p = [
            patch("src.devices.scan.discover_usb", return_value=[]),
            patch("src.devices.scan.discover_network", return_value=[]),
            patch("src.devices.scan.discover_bluetooth", return_value=[]),
        ]
        for x in p:
            x.start()
            stack.append(x)
        c = _client(config, monkeypatch, service)
        c.__enter__()
        stack.append(c)
        return c

    yield make
    for x in reversed(stack):
        if isinstance(x, TestClient):
            x.__exit__(None, None, None)
        else:
            x.stop()


def test_activation_keeps_secrets_apart_from_the_config(client_for, home):
    c = client_for(FakeService(0))
    r = c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    assert r.status_code == 200, r.text
    assert "pho_" not in r.text and "dataKey" not in r.text

    config_file = home / "offline" / "bark-01" / "config.json"
    stored = config_file.read_text(encoding="utf-8")
    # The config file holds no secret at all.
    assert "pho_secret_token_value" not in stored and "dataKey" not in stored
    # Only this user may read it.
    assert stat.S_IMODE(os.stat(config_file).st_mode) == 0o600

    full = offline_state.current_config()
    assert full["deviceToken"] == "pho_secret_token_value"
    assert len(__import__("base64").b64decode(full["dataKey"])) == 32
    assert full["dataDir"] == str(home / "offline" / "bark-01" / "data")
    assert full["deviceId"] == "till-7f3a"


def test_a_new_token_keeps_the_data_key(client_for):
    c = client_for(FakeService(0))
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    first = offline_state.current_config()["dataKey"]
    again = c.post("/offline/activate", json={**PAYLOAD, "deviceToken": "pho_renewed"}, headers=KEY)
    assert again.status_code == 200
    now = offline_state.current_config()
    # The local copy was encrypted with the first key: it must stay.
    assert now["dataKey"] == first and now["deviceToken"] == "pho_renewed"


def test_another_shop_cannot_take_over_an_active_till(client_for):
    c = client_for(FakeService(0))
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    r = c.post("/offline/activate", json={**PAYLOAD, "appid": "other-shop"}, headers=KEY)
    assert r.status_code == 400
    assert offline_state.active_appid() == "bark-01"


@pytest.mark.parametrize(
    "bad",
    [
        {"appid": "../etc"},
        {"appid": "a b"},
        {"deviceId": "x;rm"},
        {"deviceToken": "not-a-device-token"},
        {"apiBase": "http://api.petshandler.com/api"},
        {"apiBase": "https://evil.example/steal"},
    ],
)
def test_what_cannot_activate_is_refused(client_for, bad):
    c = client_for(FakeService(0))
    r = c.post("/offline/activate", json={**PAYLOAD, **bad}, headers=KEY)
    assert r.status_code == 400
    assert offline_state.active_appid() is None


def test_the_routes_need_the_managers_key(client_for):
    c = client_for(FakeService(0))
    assert c.post("/offline/activate", json=PAYLOAD).status_code in (401, 403)
    assert c.get("/offline/status").status_code in (401, 403)


def test_a_build_without_the_runtime_says_so(client_for):
    c = client_for(None)
    r = c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    assert r.status_code == 409
    assert c.get("/offline/status", headers=KEY).json()["available"] is False


def test_status_shows_the_shop_and_the_queue_but_no_secret(client_for):
    c = client_for(FakeService(3))
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    body = c.get("/offline/status", headers=KEY).json()
    assert body["activated"] and body["appid"] == "bark-01" and body["shopName"] == "Барк"
    assert body["service"]["queued"] == 3
    assert "pho_" not in json.dumps(body)


def test_switching_off_waits_for_the_queue(client_for, home):
    svc = FakeService(2)
    c = client_for(svc)
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    r = c.post("/offline/deactivate", headers=KEY)
    assert r.status_code == 409
    assert offline_state.active_appid() == "bark-01"
    assert svc.stopped == 0

    svc.queued = None  # the service does not answer: we cannot know
    assert c.post("/offline/deactivate", headers=KEY).status_code == 409


def test_switching_off_stops_the_service_and_removes_the_shop(client_for, home):
    svc = FakeService(0)
    c = client_for(svc)
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    (home / "offline" / "bark-01" / "data").mkdir()
    (home / "offline" / "bark-01" / "data" / "offline.sqlite").write_text("x")
    r = c.post("/offline/deactivate", headers=KEY)
    assert r.status_code == 200
    assert svc.stopped == 1
    assert offline_state.active_appid() is None
    assert offline_state.current_config() is None
    assert not (home / "offline" / "bark-01").exists()


def test_an_update_waits_while_offline_sales_are_unsent(client_for):
    c = client_for(FakeService(4))
    body = c.get("/busy").json()
    assert body["busy"] is True
    assert "4" in body["message"] or any("4" in r for r in body.get("reasons", []))


def test_nothing_unsent_does_not_hold_an_update(client_for):
    c = client_for(FakeService(0))
    assert c.get("/busy").json()["busy"] is False


def test_keychain_gets_the_secret_on_stdin_not_in_the_arguments(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append((args, kw.get("input")))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(offline_secrets.subprocess, "run", fake_run)
    offline_secrets._mac_save("bark-01", {"deviceToken": "pho_top_secret", "dataKey": "k"})
    args, stdin = calls[0]
    encoded = offline_secrets._encode({"deviceToken": "pho_top_secret", "dataKey": "k"})
    # Nothing secret in the process list; the whole command goes through stdin.
    assert all(encoded not in a and "pho_top_secret" not in a for a in args)
    assert encoded in stdin
