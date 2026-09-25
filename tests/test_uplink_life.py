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


# --- what the first review round found -------------------------------------


def test_a_yaml_parsed_datetime_is_understood():
    """PyYAML turns an UNQUOTED 2026-09-25T12:00:00 into a datetime.

    We write the value quoted, but the block says hand-editing is fine, and a
    person copying the timestamp back rarely adds the quotes. Catching only
    ValueError meant `fromisoformat(datetime)` raised TypeError — uncaught at
    boot, which took the whole manager down.
    """
    assert enabled_at({"uplink": {"enabled_at": NOW}}) == NOW


def test_a_naive_yaml_datetime_is_read_as_utc():
    naive = datetime(2026, 9, 25, 12, 0, 0)
    assert enabled_at({"uplink": {"enabled_at": naive}}) == NOW


def test_a_nonsense_type_does_not_raise():
    # A number, a list, whatever somebody typed — unknown means expired, not
    # a crash.
    assert enabled_at({"uplink": {"enabled_at": 12345}}) is None
    assert is_expired({"uplink": {"enabled": True, "enabled_at": 12345}}) is True


@pytest.mark.asyncio
async def test_expire_if_due_never_raises(monkeypatch):
    """It runs at boot, where an exception stops the manager starting.

    Whatever the check itself does — a value nobody anticipated, a library
    that changes its mind about an exception type — the manager must still
    come up. Left to propagate, this took the whole service down.
    """
    import src.services.uplink_life as life

    def boom(_cfg, now=None):
        raise RuntimeError("something nobody thought of")

    monkeypatch.setattr(life, "is_expired", boom)
    state = SimpleNamespace(uplink=None)
    assert await life.expire_if_due(state, cfg()) is False


@pytest.mark.asyncio
async def test_expire_if_due_closes_an_overdue_session(monkeypatch):
    saved = {}
    monkeypatch.setattr("src.routes.system.persist_uplink_state", lambda u: saved.update(u))
    from src.services.uplink_life import expire_if_due

    state = SimpleNamespace(uplink=FakeClient())
    # Deliberately long ago: `expire_if_due` reads the real clock, so a date
    # relative to this file's NOW would make the test depend on when it runs.
    c = cfg(enabled_at="2020-01-01T00:00:00+00:00")
    assert await expire_if_due(state, c) is True
    assert c["uplink"]["enabled"] is False


@pytest.mark.asyncio
async def test_a_session_switched_on_again_while_stopping_is_left_alone(monkeypatch):
    """The operator's re-enable must win over a shutdown already in flight.

    Their own request has already answered «saved». Writing «off» afterwards
    kills the support session they just opened, with nothing shown anywhere.
    """
    saved = {}
    monkeypatch.setattr("src.routes.system.persist_uplink_state", lambda u: saved.update(u))

    c = cfg()
    state = SimpleNamespace(uplink=None)

    class SlowClient(FakeClient):
        async def stop(self):
            # While we are tearing the old socket down, the dashboard opens a
            # new session.
            c["uplink"]["enabled_at"] = (NOW + timedelta(minutes=5)).isoformat()
            c["uplink"]["enabled"] = True
            state.uplink = FakeClient()
            await super().stop()

    state.uplink = SlowClient()
    assert await shut_down(state, c, "a day has passed") is False
    assert c["uplink"]["enabled"] is True
    assert state.uplink is not None
    assert saved == {}


# ---------------------------------------------------------------------------
# PET-928 — the dashboard switch is not a fourth way to do the same thing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_dashboard_off_switch_goes_through_the_shared_shutdown(
    monkeypatch, tmp_path,
):
    """Switching off from the dashboard must use `shut_down`, not its own copy.

    It used to stop the client inline, which made four places doing the same
    steps: the countdown, the boot check, the order from the server and this.
    Four copies is how the singleton ends up cleared in three of them and left
    dangling in the fourth.
    """
    from types import SimpleNamespace

    import src.routes.system as system
    import src.services.uplink_life as life

    calls = []

    async def fake_shut_down(app_state, cfg, reason):
        calls.append(reason)
        app_state.uplink = None
        return True

    monkeypatch.setattr(life, "shut_down", fake_shut_down)
    monkeypatch.setattr(system, "_CONFIG_PATH", tmp_path / "config.yaml")

    class Client:
        def __init__(self):
            self.stopped = False

        async def stop(self):
            self.stopped = True

        def detach_handler_from_root(self):
            pass

    client = Client()
    cfg = {"uplink": {"enabled": True, "tenant": "shop", "enabled_at": "2026-09-25T10:00:00+00:00"}}
    state = SimpleNamespace(config=cfg, uplink=client)
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    await system.set_uplink(system.UplinkPayload(enabled=False), request)

    assert calls == ["dashboard switch"], "the switch did its own shutdown again"
    # And it did NOT also stop the client by hand — that is the shared
    # function's job, and doing both is how the two drift apart.
    assert client.stopped is False
    assert state.uplink is None


@pytest.mark.asyncio
async def test_switching_off_from_the_dashboard_really_stops_the_client(
    monkeypatch, tmp_path,
):
    """The same path, end to end, with the real `shut_down` underneath."""
    from types import SimpleNamespace

    import src.routes.system as system

    monkeypatch.setattr(system, "_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(system, "persist_uplink_state", lambda u: None)
    monkeypatch.setattr(
        "src.services.uplink_life.persist_uplink_state", lambda u: None, raising=False,
    )

    stopped = []
    detached = []

    class Client:
        async def stop(self):
            stopped.append(True)

        def detach_handler_from_root(self):
            detached.append(True)

    client = Client()
    cfg = {"uplink": {"enabled": True, "tenant": "shop", "enabled_at": "2026-09-25T10:00:00+00:00"}}
    state = SimpleNamespace(config=cfg, uplink=client)
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    await system.set_uplink(system.UplinkPayload(enabled=False), request)

    assert stopped == [True]
    assert detached == [True]
    assert state.uplink is None
    assert cfg["uplink"]["enabled"] is False
    assert cfg["uplink"]["enabled_at"] == ""
