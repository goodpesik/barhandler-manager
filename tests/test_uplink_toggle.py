"""Tests for the config.yaml surgical edit + tenant origin extraction."""

import yaml

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
