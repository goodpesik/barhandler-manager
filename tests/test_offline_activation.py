"""PET-972 — offline mode is switched on from Petshandler and kept here.

The online app takes a device token from the server and hands it to the
manager; the manager keeps the secrets and the rest of the config in private
files, and refuses to switch off (or to update) while
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
        # Only the product's own servers: the manager's API key is public.
        {"apiBase": "https://evil.com/api"},
        {"apiBase": "https://api.petshandler.com.evil.com/api"},
        {"apiBase": "https://api.petshandler.com:8443/api"},
        # A newline would split a `security -i` line into two commands.
        {"appid": "bark-01\n"},
        {"deviceId": "till\n"},
        # Other products join PRODUCTS first (FitStudio, BarHandler: planned).
        {"product": "barhandler"},
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


def test_secrets_are_one_private_file_and_no_system_store_is_asked(client_for, home, monkeypatch):
    """The Keychain locked after sleep and stopped the till: no system store at all."""
    import subprocess as sp

    def no_store(*a, **k):
        raise AssertionError(f"a system store was asked: {a}")

    monkeypatch.setattr(sp, "run", no_store)
    c = client_for(FakeService(0))
    assert c.post("/offline/activate", json=PAYLOAD, headers=KEY).status_code == 200
    kept = home / "offline" / "bark-01" / "secrets.b64"
    assert stat.S_IMODE(os.stat(kept).st_mode) == 0o600
    assert "pho_secret_token_value" not in kept.read_text()
    assert offline_state.current_config()["deviceToken"] == "pho_secret_token_value"
    # A new token for the same shop keeps the data key.
    key = offline_state.current_config()["dataKey"]
    c.post("/offline/activate", json={**PAYLOAD, "deviceToken": "pho_renewed"}, headers=KEY)
    assert offline_state.current_config()["dataKey"] == key
    assert offline_state.current_config()["deviceToken"] == "pho_renewed"


def test_the_product_is_recorded_and_its_runtime_used(client_for, home):
    from src.services.offline_service import runtime_dir

    c = client_for(FakeService(0))
    r = c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    assert r.json()["product"] == "petshandler"
    assert c.get("/offline/status", headers=KEY).json()["product"] == "petshandler"
    assert offline_state.current_config()["staticDir"] == str(runtime_dir("petshandler") / "app")
    assert runtime_dir("petshandler").name == "petshandler"


# ---- PET-973: the browser settings snapshot ------------------------------


def test_the_online_apps_settings_are_kept_for_the_offline_build(client_for, home):
    c = client_for(FakeService(0))
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    items = {"phm.manager.fiscalPrinter": "p1", "petshandler:lang": "uk"}
    r = c.post("/offline/local-settings", json={"appid": "bark-01", "items": items}, headers=KEY)
    assert r.status_code == 200 and r.json()["count"] == 2
    got = c.get("/offline/local-settings", headers=KEY).json()
    assert got["items"] == items and got["savedAt"] and got["id"]
    # A change is a new snapshot for the offline build.
    again = c.post("/offline/local-settings", json={"appid": "bark-01", "items": {**items, "petshandler:lang": "en"}}, headers=KEY)
    assert again.json()["id"] != got["id"]
    kept = home / "offline" / "bark-01" / "local-settings.json"
    assert stat.S_IMODE(os.stat(kept).st_mode) == 0o600


def test_settings_of_another_shop_or_before_activation_are_refused(client_for):
    c = client_for(FakeService(0))
    body = {"appid": "bark-01", "items": {"a": "b"}}
    assert c.post("/offline/local-settings", json=body, headers=KEY).status_code == 409
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    other = {"appid": "other", "items": {"a": "b"}}
    assert c.post("/offline/local-settings", json=other, headers=KEY).status_code == 409
    assert c.get("/offline/local-settings", headers=KEY).status_code == 404


@pytest.mark.parametrize(
    "items",
    [
        ["not", "a", "dict"],
        {"key with spaces": "v"},
        {"k": 1},
        {"k": "x" * 70_000},
        # The manager's own address may only be this computer.
        {"phm.manager.url": "https://evil.example"},
        {"phm.manager.url": "http://localhost:9999\n"},
    ],
)
def test_settings_that_are_not_a_small_string_map_are_refused(client_for, items):
    c = client_for(FakeService(0))
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    r = c.post("/offline/local-settings", json={"appid": "bark-01", "items": items}, headers=KEY)
    assert r.status_code == 409
    assert c.get("/offline/local-settings", headers=KEY).status_code == 404


def test_switching_off_removes_the_settings_too(client_for, home):
    c = client_for(FakeService(0))
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    c.post("/offline/local-settings", json={"appid": "bark-01", "items": {"a": "b"}}, headers=KEY)
    c.post("/offline/deactivate", headers=KEY)
    assert c.get("/offline/local-settings", headers=KEY).status_code == 404



# ---- review round 1 (PET-972) --------------------------------------------


@pytest.mark.parametrize("api", ["https://api.petshandler.com/api", "https://api-dev.petshandler.com/api/", "https://API.petshandler.com/api"])
def test_the_products_own_servers_are_accepted(client_for, api):
    c = client_for(FakeService(0))
    assert c.post("/offline/activate", json={**PAYLOAD, "apiBase": api}, headers=KEY).status_code == 200


def test_secrets_that_will_not_delete_keep_the_shop_on(client_for, monkeypatch):
    c = client_for(FakeService(0))
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)

    def refuse(appid, folder):
        raise OSError("secrets file could not be removed")

    monkeypatch.setattr(offline_secrets, "delete", refuse)
    r = c.post("/offline/deactivate", headers=KEY)
    assert r.status_code == 500
    # Nothing half-done: still active, secrets and data in place.
    assert offline_state.active_appid() == "bark-01"
    assert offline_state.current_config() is not None


def test_a_service_that_fails_to_stop_still_leaves_no_unreadable_data(client_for, home):
    svc = FakeService(0)

    async def broken_stop():
        raise OSError("terminate failed")

    svc.stop_service = broken_stop
    c = client_for(svc)
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    (home / "offline" / "bark-01" / "data").mkdir()
    r = c.post("/offline/deactivate", headers=KEY)
    assert r.status_code == 200
    # The key is gone, so the copy it encrypted goes too.
    assert offline_state.active_appid() is None
    assert not (home / "offline" / "bark-01").exists()



def test_a_local_manager_address_is_kept(client_for):
    c = client_for(FakeService(0))
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    for url in ("http://localhost:9999", "http://127.0.0.1:9999/"):
        r = c.post("/offline/local-settings", json={"appid": "bark-01", "items": {"phm.manager.url": url}}, headers=KEY)
        assert r.status_code == 200, url


# ---- review round 2 (PET-972 / PET-973) ------------------------------------


def test_a_copy_left_without_its_key_is_moved_aside_on_activation(client_for, home):
    c = client_for(FakeService(0))
    old = home / "offline" / "bark-01" / "data"
    old.mkdir(parents=True)
    (old / "offline.sqlite").write_text("encrypted with a key that is gone")
    r = c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    assert r.status_code == 200
    # The new key never meets the old copy.
    assert not (home / "offline" / "bark-01" / "data").exists()
    aside = [p for p in (home / "offline" / "bark-01").iterdir() if p.name.startswith("data.unreadable-")]
    assert len(aside) == 1 and (aside[0] / "offline.sqlite").exists()


def test_an_unreadable_secrets_file_stops_the_activation_and_moves_nothing(client_for, home):
    """A file that cannot be read now may hold the data key: it is not «no key»."""
    c = client_for(FakeService(0))
    assert c.post("/offline/activate", json=PAYLOAD, headers=KEY).status_code == 200
    shop = home / "offline" / "bark-01"
    (shop / "data").mkdir()
    (shop / "data" / "offline.sqlite").write_text("encrypted with the kept key")
    secrets = shop / "secrets.b64"
    kept = secrets.read_text()
    secrets.unlink()
    secrets.mkdir()  # reading it now fails with an OSError, not «not found»
    r = c.post("/offline/activate", json={**PAYLOAD, "deviceToken": "pho_renewed"}, headers=KEY)
    assert r.status_code == 500 and r.json()["detail"]["code"] == "secrets_unreadable"
    assert (shop / "data" / "offline.sqlite").exists()
    assert not [p for p in shop.iterdir() if p.name.startswith("data.unreadable-")]
    # Readable again: the same key, the copy untouched.
    secrets.rmdir()
    secrets.write_text(kept)
    assert c.post("/offline/activate", json={**PAYLOAD, "deviceToken": "pho_renewed"}, headers=KEY).status_code == 200
    assert offline_secrets._decode(secrets.read_text())["dataKey"] == offline_secrets._decode(kept)["dataKey"]
    assert (shop / "data" / "offline.sqlite").exists()


def test_current_config_says_unreadable_rather_than_not_activated(client_for, home):
    c = client_for(FakeService(0))
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    secrets = home / "offline" / "bark-01" / "secrets.b64"
    secrets.unlink()
    secrets.mkdir()
    with pytest.raises(offline_secrets.SecretsUnreadable):
        offline_state.current_config()


@pytest.mark.parametrize("stored", ["[]", "{}", '{"apiBase": "https://api.petshandler.com/api"}'])
def test_an_incomplete_config_is_not_activated_rather_than_an_error(client_for, home, stored):
    c = client_for(FakeService(0))
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    (home / "offline" / "bark-01" / "config.json").write_text(stored)
    assert offline_state.current_config() is None


def test_a_damaged_secrets_file_is_no_secrets(client_for, home):
    c = client_for(FakeService(0))
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    (home / "offline" / "bark-01" / "secrets.b64").write_bytes(b"\xff\xfe not base64")
    assert offline_state.current_config() is None


def test_a_failed_write_leaves_no_temporary_file_with_a_secret(home, monkeypatch):
    folder = home / "offline" / "bark-01"

    def broken_replace(a, b):
        raise OSError("disk full")

    monkeypatch.setattr(offline_secrets.os, "replace", broken_replace)
    with pytest.raises(OSError):
        offline_secrets.save("bark-01", {"deviceToken": "pho_x", "dataKey": "k"}, folder)
    assert [p.name for p in folder.iterdir()] == []


def test_a_short_write_is_finished(home, monkeypatch):
    real_write = os.write
    monkeypatch.setattr(offline_secrets.os, "write", lambda fd, data: real_write(fd, bytes(data[:3])))
    folder = home / "offline" / "bark-01"
    offline_secrets.save("bark-01", {"deviceToken": "pho_x", "dataKey": "k"}, folder)
    assert offline_secrets.load("bark-01", folder) == {"deviceToken": "pho_x", "dataKey": "k"}


def test_a_lost_active_marker_does_not_throw_the_key_away(client_for, home):
    c = client_for(FakeService(0))
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    key = offline_state.current_config()["dataKey"]
    (home / "offline" / "active.json").unlink()
    assert c.post("/offline/activate", json=PAYLOAD, headers=KEY).status_code == 200
    assert offline_state.current_config()["dataKey"] == key


def test_the_same_settings_keep_their_snapshot_id(client_for):
    c = client_for(FakeService(0))
    c.post("/offline/activate", json=PAYLOAD, headers=KEY)
    body = {"appid": "bark-01", "items": {"phm.manager.fiscalPrinter": "p1"}}
    first = c.post("/offline/local-settings", json=body, headers=KEY).json()["id"]
    # An online reload sends the same settings again: nothing new to apply.
    assert c.post("/offline/local-settings", json=body, headers=KEY).json()["id"] == first
    changed = {"appid": "bark-01", "items": {"phm.manager.fiscalPrinter": "p2"}}
    assert c.post("/offline/local-settings", json=changed, headers=KEY).json()["id"] != first



def test_an_activation_sent_twice_at_once_does_not_fail(home):
    import threading

    old = home / "offline" / "bark-01" / "data"
    old.mkdir(parents=True)
    (old / "offline.sqlite").write_text("x")
    errors = []

    def go():
        try:
            offline_state.activate(dict(PAYLOAD))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert offline_state.active_appid() == "bark-01"
