"""Tests for the surgical config.yaml edit done by POST /system/uplink.

We don't test the SIGTERM / restart half — that's a one-line bridge to
the OS service manager.
"""

import yaml

from src.routes.system import (
    UplinkPayload,
    _render_uplink_block,
    _replace_uplink_in_config,
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
    new = _replace_uplink_in_config(_BASE_CONFIG, UplinkPayload(
        enabled=True, tenant="biergarten-lviv.barhandler.com",
        url="https://manager.barhandler.com",
    ))
    parsed = _parse(new)
    assert parsed["uplink"]["enabled"] is True
    assert parsed["uplink"]["tenant"] == "biergarten-lviv.barhandler.com"
    assert parsed["uplink"]["url"] == "https://manager.barhandler.com"
    # Pre-existing keys untouched
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
    new = _replace_uplink_in_config(text, UplinkPayload(
        enabled=True, tenant="t.barhandler.com",
        url="https://manager.barhandler.com",
    ))
    parsed = _parse(new)
    assert parsed["uplink"]["enabled"] is True
    assert parsed["uplink"]["tenant"] == "t.barhandler.com"
    # Crucially: the file should NOT contain the old commented uplink
    # block any more (otherwise yaml will resolve the active block, but
    # operators reading the file would see contradictory entries).
    # Count `uplink:` occurrences across all lines (active + commented).
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
    new = _replace_uplink_in_config(text, UplinkPayload(enabled=False))
    parsed = _parse(new)
    assert parsed["uplink"]["enabled"] is False
    # Pre-existing keys untouched
    assert parsed["server"]["port"] == 9999


def test_round_trip_preserves_other_sections():
    new = _replace_uplink_in_config(_BASE_CONFIG, UplinkPayload(
        enabled=True, tenant="t.barhandler.com",
    ))
    parsed = _parse(new)
    assert parsed["server"]["port"] == 9999
    assert parsed["devices"]["receipt"]["enabled"] is True


def test_payload_rejects_non_fqdn_tenant():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        UplinkPayload(enabled=True, tenant='evil; rm -rf /')


def test_render_block_is_valid_yaml():
    block = _render_uplink_block(UplinkPayload(
        enabled=True, tenant="t.barhandler.com",
    ))
    standalone = "server:\n  port: 9999\n\n" + block
    parsed = _parse(standalone)
    assert parsed["uplink"]["enabled"] is True
    assert parsed["uplink"]["tenant"] == "t.barhandler.com"
