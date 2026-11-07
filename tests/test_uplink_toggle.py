"""Tests for the config.yaml surgical edit + tenant origin extraction."""

import builtins
import io

import pytest
import yaml

from src.config import load_config
from src.routes import system
from src.routes.system import (
    _render_uplink_block,
    _replace_uplink_in_config,
    _extract_tenant_from_origin,
)


_BASE_CONFIG = """\
server:
  port: 9999

devices:
  receipt:
    enabled: true
    paper_width: 58
"""


def _parse(text: str) -> dict:
    return yaml.safe_load(text) or {}


def test_appends_when_no_existing_block():
    new = _replace_uplink_in_config(
        _BASE_CONFIG, enabled=True, tenant="biergarten-lviv.barhandler.com",
    )
    parsed = _parse(new)
    assert parsed["uplink"]["enabled"] is True
    assert parsed["uplink"]["tenant"] == "biergarten-lviv.barhandler.com"
    assert parsed["uplink"]["url"] == "https://manager.barhandler.com"
    assert parsed["server"]["port"] == 9999
    assert parsed["devices"]["receipt"]["paper_width"] == 58


def test_replaces_commented_block_at_end():
    text = _BASE_CONFIG + """
# Remote log uplink. When enabled, the manager streams its bhm.log lines
# and business events to the central server, and accepts whitelist
# diagnostic commands from there.
# uplink:
#   enabled: false
#   url: "https://manager.barhandler.com"
#   tenant: ""
#   reconnect_delay: 2
"""
    new = _replace_uplink_in_config(text, enabled=True, tenant="t.barhandler.com")
    parsed = _parse(new)
    assert parsed["uplink"]["enabled"] is True
    assert parsed["uplink"]["tenant"] == "t.barhandler.com"
    uplink_line_count = sum(
        1 for ln in new.splitlines() if ln.strip().startswith(("uplink:", "# uplink:"))
    )
    assert uplink_line_count == 1


def test_replaces_active_block_when_toggling_off():
    text = _BASE_CONFIG + """
# Remote log uplink — managed by the dashboard.
uplink:
  enabled: true
  url: "https://manager.barhandler.com"
  tenant: "t.barhandler.com"
  reconnect_delay: 2
"""
    new = _replace_uplink_in_config(text, enabled=False, tenant="")
    parsed = _parse(new)
    assert parsed["uplink"]["enabled"] is False
    assert parsed["server"]["port"] == 9999


def test_round_trip_preserves_other_sections():
    new = _replace_uplink_in_config(_BASE_CONFIG, enabled=True, tenant="t.barhandler.com")
    parsed = _parse(new)
    assert parsed["server"]["port"] == 9999
    assert parsed["devices"]["receipt"]["enabled"] is True


def test_render_block_is_valid_yaml():
    block = _render_uplink_block(enabled=True, tenant="t.barhandler.com")
    standalone = "server:\n  port: 9999\n\n" + block
    parsed = _parse(standalone)
    assert parsed["uplink"]["enabled"] is True
    assert parsed["uplink"]["tenant"] == "t.barhandler.com"


def test_extract_tenant_from_origin_strips_scheme_and_port():
    assert _extract_tenant_from_origin("https://biergarten-lviv.barhandler.com") == "biergarten-lviv.barhandler.com"
    assert _extract_tenant_from_origin("https://app.fitstudiocrm.com:443") == "app.fitstudiocrm.com"
    assert _extract_tenant_from_origin("http://localhost:9999") == "localhost"


def test_extract_tenant_returns_none_for_garbage():
    assert _extract_tenant_from_origin("") is None
    assert _extract_tenant_from_origin("javascript:alert(1)") is None
    assert _extract_tenant_from_origin("not a url") is None


# --- PET-1304: the whole file is rewritten on every toggle ---------------
#
# `config.yaml` carries the server port, the API key the app authenticates
# with and the uplink tenant. A bare write truncates the file first, so a
# kill or a power cut in the middle of it left a half-written file behind,
# the manager came up on defaults and the shop dropped off the dashboard.

# A port that is NOT the built-in default, so losing the file cannot pass
# for keeping it: `load_config` fills 9999 in from nothing at all.
_LIVE_CONFIG = """\
server:
  port: 9710
  api_key: "the-shop-key"

devices:
  receipt:
    enabled: true
"""


class _DyingHandle:
    """A file handle that takes nothing and dies.

    Stands in for the station going down in the middle of the write.
    Opening for writing has ALREADY truncated whatever was there — that
    is what mode "w" does, before a single byte is handed over — so a
    write aimed straight at `config.yaml` has lost it by this point,
    while one aimed at a temporary file leaves `config.yaml` as it was.
    """

    def __init__(self, handle):
        self._handle = handle

    def write(self, text: str) -> int:
        raise OSError("the station lost power mid-write")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()
        return False

    def __getattr__(self, name):
        return getattr(self._handle, name)


def _die_on_writing_to(monkeypatch, config_path) -> None:
    """Every write opened under `config_path`'s name dies half-way.

    Both doors are held, because `open(...)` and `Path.write_text()` do
    not go through the same one: the builtin is its own binding, while
    pathlib looks `open` up on `io` at every call. Anything else is let
    straight through, so only this one file is affected.
    """
    real_open = builtins.open

    def dying_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        if "w" in mode and str(file).startswith(str(config_path)):
            return _DyingHandle(handle)
        return handle

    monkeypatch.setattr(builtins, "open", dying_open)
    monkeypatch.setattr(io, "open", dying_open)


def test_a_write_that_dies_midway_leaves_the_stored_config_intact(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(_LIVE_CONFIG, encoding="utf-8")
    monkeypatch.setattr(system, "_CONFIG_PATH", config)
    _die_on_writing_to(monkeypatch, config)

    with pytest.raises(OSError):
        system.persist_uplink_state({
            "enabled": True,
            "tenant": "kava-lviv.barhandler.com",
        })

    monkeypatch.undo()
    stored = load_config(str(config))
    assert stored["server"]["port"] == 9710
    assert stored["server"]["api_key"] == "the-shop-key"


def test_a_cyrillic_shop_label_survives_the_write_and_reads_back(tmp_path, monkeypatch):
    # The write and the read have to agree on the encoding: `load_config`
    # reads UTF-8, so the write is UTF-8 too. Given the station's own code
    # page instead, a Ukrainian shop name comes back as mojibake or the
    # whole file stops parsing — which reads as a manager with no config.
    config = tmp_path / "config.yaml"
    config.write_text(_LIVE_CONFIG, encoding="utf-8")
    monkeypatch.setattr(system, "_CONFIG_PATH", config)

    system.persist_uplink_state({
        "enabled": True,
        "tenant": "kava-lviv.barhandler.com",
        "tenant_name": "Кава «Львів»",
    })

    stored = load_config(str(config))
    assert stored["uplink"]["tenant_name"] == "Кава «Львів»"
    assert stored["server"]["api_key"] == "the-shop-key"
