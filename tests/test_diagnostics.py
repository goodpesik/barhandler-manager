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
