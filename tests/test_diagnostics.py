import pytest

from src.services.diagnostics import run_diagnostic


@pytest.mark.asyncio
async def test_unknown_cmd():
    r = await run_diagnostic("nope", {})
    assert r["ok"] is False
    assert "unknown" in r["error"].lower()


@pytest.mark.asyncio
async def test_dump_config_redacts():
    cfg = {"server": {"api_key": "SECRET", "port": 9999}, "uplink": {"enabled": True}}
    r = await run_diagnostic("dump_config", {}, config=cfg)
    assert r["ok"] is True
    assert "SECRET" not in r["output"]
    assert "***" in r["output"]


@pytest.mark.asyncio
async def test_dump_config_without_context():
    r = await run_diagnostic("dump_config", {})
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_ping_validates_host():
    r = await run_diagnostic("ping", {"host": "evil; rm -rf /"})
    assert r["ok"] is False
    assert "invalid host" in r["error"].lower()


@pytest.mark.asyncio
async def test_list_interfaces_returns_addresses():
    r = await run_diagnostic("list_interfaces", {})
    assert r["ok"] is True
    assert isinstance(r["output"], str)
    assert len(r["output"]) > 0


@pytest.mark.asyncio
async def test_terminal_probe_invalid_ip():
    r = await run_diagnostic("terminal_probe", {"ip": "; evil"})
    assert r["ok"] is False
    assert "invalid ip" in r["error"].lower()


@pytest.mark.asyncio
async def test_tail_log_invalid_n():
    r = await run_diagnostic("tail_log", {"n": "abc"})
    assert r["ok"] is False


# --- PET-928: switching remote diagnostics off from the server -------------


@pytest.mark.asyncio
async def test_uplink_off_is_not_an_unknown_command():
    """It travels the same socket as every other diagnostic command.

    The one it is NOT is a plain entry in the command table: closing the door
    needs the running client, which the table's functions cannot reach.
    """
    from types import SimpleNamespace

    from src.services.diagnostics import make_callback

    cfg = {"uplink": {"enabled": True, "enabled_at": "2026-09-25T12:00:00+00:00"}}
    state = SimpleNamespace(uplink=None)
    cb = make_callback(cfg, state)
    r = await cb("cmd-1", "uplink_off", {})
    assert r["ok"] is True
    assert r["cmd_id"] == "cmd-1"


@pytest.mark.asyncio
async def test_uplink_off_answers_BEFORE_it_stops_the_socket(monkeypatch):
    """The reply leaves first; afterwards there is nothing to answer on."""
    import asyncio
    from types import SimpleNamespace

    from src.services.diagnostics import make_callback

    stopped = []

    class Client:
        async def stop(self):
            stopped.append("stopped")

        def detach_handler_from_root(self):
            pass

    monkeypatch.setattr("src.routes.system.persist_uplink_state", lambda u: None)
    cfg = {"uplink": {"enabled": True, "enabled_at": "2026-09-25T12:00:00+00:00"}}
    state = SimpleNamespace(uplink=Client())
    cb = make_callback(cfg, state)

    r = await cb("cmd-2", "uplink_off", {})
    assert r["ok"] is True
    # Still up at the moment of the answer.
    assert stopped == []
    await asyncio.sleep(0.7)
    assert stopped == ["stopped"]
    assert cfg["uplink"]["enabled"] is False


@pytest.mark.asyncio
async def test_uplink_off_says_so_when_it_cannot_reach_the_manager():
    from src.services.diagnostics import make_callback

    cb = make_callback({"uplink": {"enabled": True}}, None)
    r = await cb("cmd-3", "uplink_off", {})
    assert r["ok"] is False
    assert "state" in r["error"]


@pytest.mark.asyncio
async def test_an_ordinary_command_still_works_through_the_same_callback():
    # The mirror: routing `uplink_off` specially must not swallow the rest.
    from src.services.diagnostics import make_callback

    cfg = {"server": {"api_key": "SECRET"}, "uplink": {"enabled": True}}
    cb = make_callback(cfg, None)
    r = await cb("cmd-4", "dump_config", {})
    assert r["ok"] is True
    assert "SECRET" not in r["output"]
