"""PET-928 — the expiry decision is made BEFORE the socket is built.

This is the one part of the feature that lives in `src/server.py`'s lifespan,
and it is there for a reason that a unit test of `uplink_life` cannot show: a
client that has already been told to connect cannot reliably be told to stop.
`disconnect()` on a client that has not finished connecting does nothing, and
the `start()` task then goes on to connect anyway — leaving a live remote
session that nothing holds a handle on.

So the assertion is not «it was stopped». It is that the constructor is never
reached at all.
"""

from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from src.server import create_app


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _config(enabled_at: str, tmp_path) -> dict:
    return {
        "server": {
            "port": 9999,
            "registry_path": str(tmp_path / "printers.json"),
            "terminal_registry_path": str(tmp_path / "terminals.json"),
        },
        "uplink": {
            "enabled": True,
            "url": "https://logs.example",
            "tenant": "shop",
            "enabled_at": enabled_at,
        },
    }


@pytest.fixture
def built(monkeypatch):
    """Record every LogUplinkClient the boot builds, and build nothing real."""
    made = []

    class FakeClient:
        def __init__(self, cfg):
            made.append(cfg)

        def attach_handler_to_root(self):
            pass

        def detach_handler_from_root(self):
            pass

        def set_diagnostics_callback(self, cb):
            pass

        async def start(self, install_id, version):
            pass

        async def stop(self):
            pass

    import src.services.log_uplink as lu
    import src.services.uplink_life as life
    from src.services.update_check import UpdateChecker

    monkeypatch.setattr(lu, "LogUplinkClient", FakeClient)
    monkeypatch.setattr(lu, "set_active", lambda c: None)
    # The config file is not what this test is about, and writing it would
    # reach outside the test's directory.
    monkeypatch.setattr("src.routes.system.persist_uplink_state", lambda u: None)
    monkeypatch.setattr(life, "_CHECK_INTERVAL_SEC", 3600)

    async def no_probe(self):
        return None

    monkeypatch.setattr(UpdateChecker, "check_once", no_probe)
    return made


def test_an_expired_session_is_never_built(built, tmp_path):
    """Switched on eight days ago, then the machine was off. It stays off."""
    long_ago = _iso(datetime.now(timezone.utc) - timedelta(days=8))
    cfg = _config(long_ago, tmp_path)

    app = create_app(cfg)
    with TestClient(app):
        pass

    assert built == [], "an expired session must not reach the constructor"
    assert cfg["uplink"]["enabled"] is False
    assert cfg["uplink"]["enabled_at"] == ""


def test_a_session_still_within_its_day_is_built(built, tmp_path):
    """The other half — without this, «never build» would pass by doing nothing.

    Mutation this catches: making `is_expired` always true, or dropping the
    `enabled` check, would silently switch remote diagnostics off for everyone
    who legitimately has them on.
    """
    just_now = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    cfg = _config(just_now, tmp_path)

    app = create_app(cfg)
    with TestClient(app):
        pass

    assert len(built) == 1
    assert cfg["uplink"]["enabled"] is True


def test_a_session_with_no_start_time_is_treated_as_expired(built, tmp_path):
    """`enabled: true` and nothing saying when — an upgrade from before this
    feature, or a hand-edited config. An unknown start time cannot be shown to
    be within its day, and the safe reading of «unknown» is «over»."""
    cfg = _config("", tmp_path)

    app = create_app(cfg)
    with TestClient(app):
        pass

    assert built == []
    assert cfg["uplink"]["enabled"] is False


def test_the_countdown_watcher_starts_even_when_the_session_is_off(
    built, tmp_path, monkeypatch,
):
    """The watcher is what closes a door that is opened later in this same run.

    Started inside the `enabled` branch it would only ever count down a session
    that was already on at boot — and the ordinary case is the opposite: the
    manager starts with diagnostics off and somebody switches them on from the
    dashboard an hour later. That session would then never time out.
    """
    import src.services.uplink_life as life

    watched = []

    async def fake_watch(app_state, cfg):
        watched.append(cfg)

    monkeypatch.setattr(life, "watch", fake_watch)

    cfg = _config("", tmp_path)  # expired, so the uplink branch is skipped
    app = create_app(cfg)
    with TestClient(app):
        pass

    assert built == [], "precondition: this boot built no client"
    assert watched == [cfg], "the countdown must run regardless of the branch above"
