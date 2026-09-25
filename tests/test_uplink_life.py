"""PET-928 — remote diagnostics close their own door.

They let support read this machine's logs and run commands on it, and in
practice they are switched on for one problem and stay on for months. So the
session lives a day, counted from when it was opened and kept on disk, and the
same shutdown can be ordered from the server — the person who asked to open it
is rarely the one sitting at the machine.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.services.uplink_life import (
    UPLINK_LIFETIME,
    enabled_at,
    expires_at,
    is_expired,
    shut_down,
)

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def cfg(**uplink):
    base = {"enabled": True, "enabled_at": NOW.isoformat(), "tenant": "t"}
    base.update(uplink)
    return {"uplink": base}


class FakeClient:
    def __init__(self):
        self.stopped = False
        self.detached = False

    async def stop(self):
        self.stopped = True

    def detach_handler_from_root(self):
        self.detached = True


def test_a_fresh_session_is_not_expired():
    assert is_expired(cfg(), now=NOW + timedelta(hours=1)) is False


def test_a_session_older_than_a_day_is_expired():
    assert is_expired(cfg(), now=NOW + UPLINK_LIFETIME) is True


def test_the_boundary_belongs_to_the_day_before():
    # A second short of the day is still open; the day itself closes it.
    assert is_expired(cfg(), now=NOW + UPLINK_LIFETIME - timedelta(seconds=1)) is False


def test_an_uplink_that_is_off_never_expires():
    # Nothing to close, and saying «expired» would make the watchdog write the
    # config on every tick for ever.
    assert is_expired(cfg(enabled=False)) is False


def test_enabled_with_no_start_time_counts_as_expired():
    """A config from before this existed, or one somebody edited.

    «We do not know when this door was opened» reads as «close it» — the other
    way round leaves it open for ever, which is the thing being fixed.
    """
    assert is_expired(cfg(enabled_at="")) is True


def test_a_broken_start_time_counts_as_expired_too():
    assert is_expired(cfg(enabled_at="yesterday-ish")) is True


def test_a_naive_timestamp_is_read_as_utc():
    # Older configs were written without an offset; they must not be read as
    # local time and given hours of extra life.
    c = cfg(enabled_at="2026-09-25T12:00:00")
    assert enabled_at(c) == NOW
    assert expires_at(c) == NOW + UPLINK_LIFETIME


@pytest.mark.asyncio
async def test_shutting_down_stops_the_client_and_clears_the_state(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        "src.routes.system.persist_uplink_state",
        lambda u: saved.update(u),
    )
    client = FakeClient()
    state = SimpleNamespace(uplink=client)
    c = cfg()

    assert await shut_down(state, c, "test") is True
    assert client.stopped and client.detached
    assert state.uplink is None
    assert c["uplink"]["enabled"] is False
    # The start time is cleared too: leaving it would make the next switch-on
    # inherit an already-expired clock.
    assert c["uplink"]["enabled_at"] == ""
    assert saved["enabled"] is False


@pytest.mark.asyncio
async def test_shutting_down_an_already_off_uplink_does_nothing(monkeypatch):
    monkeypatch.setattr("src.routes.system.persist_uplink_state", lambda u: None)
    state = SimpleNamespace(uplink=None)
    assert await shut_down(state, cfg(enabled=False), "test") is False


@pytest.mark.asyncio
async def test_the_door_closes_even_if_the_config_cannot_be_written(monkeypatch):
    """An unwritable config is worth a loud log line, not an open door."""
    def boom(_u):
        raise OSError("read-only file system")

    monkeypatch.setattr("src.routes.system.persist_uplink_state", boom)
    client = FakeClient()
    state = SimpleNamespace(uplink=client)
    c = cfg()

    assert await shut_down(state, c, "test") is True
    assert client.stopped
    assert state.uplink is None
    assert c["uplink"]["enabled"] is False


@pytest.mark.asyncio
async def test_the_door_closes_even_if_stopping_the_client_throws(monkeypatch):
    monkeypatch.setattr("src.routes.system.persist_uplink_state", lambda u: None)

    class Angry(FakeClient):
        async def stop(self):
            raise RuntimeError("socket already gone")

    state = SimpleNamespace(uplink=Angry())
    c = cfg()
    assert await shut_down(state, c, "test") is True
    assert state.uplink is None
    assert c["uplink"]["enabled"] is False
