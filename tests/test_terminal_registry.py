"""TerminalRegistry persistence + lookup behaviour.

Mirrors the printer-registry tests — registering a discovered
descriptor, reading it back, persistence across instances via the
JSON file, unregister, and the UnknownTerminal error path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.devices.terminal_registry import TerminalRegistry, UnknownTerminal
from src.models.terminal import (
    MerchantBinding,
    TerminalDescriptor,
    TerminalKind,
    TerminalNetworkAddress,
    TerminalRegistrationRequest,
    TerminalTransport,
)
from src.services.terminals.ssi import SSITerminalAdapter


def _descriptor(host: str = "10.0.0.42", port: int = 3000, sid: str = "abc123") -> TerminalDescriptor:
    return TerminalDescriptor(
        id=sid,
        transport=TerminalTransport.network,
        label=f"Mock @ {host}",
        kind=TerminalKind.mono_pos,
        model="Verifone X990",
        serial="V1E0207420",
        network=TerminalNetworkAddress(host=host, port=port),
    )


def test_register_after_discover_persists_to_disk(tmp_path: Path) -> None:
    registry = TerminalRegistry(path=tmp_path / "terminals.json")
    descriptor = _descriptor()
    registry.remember_descriptors([descriptor])

    reg = registry.register(TerminalRegistrationRequest(
        id=descriptor.id,
        kind=TerminalKind.mono_pos,
        nickname="Бар 1",
        default_merchant_id="000000060007176",
    ))

    assert reg.descriptor.id == descriptor.id
    assert reg.nickname == "Бар 1"
    # File on disk has it too
    raw = json.loads((tmp_path / "terminals.json").read_text())
    assert raw["terminals"][0]["descriptor"]["id"] == descriptor.id
    assert raw["terminals"][0]["default_merchant_id"] == "000000060007176"


def test_register_without_discover_fails(tmp_path: Path) -> None:
    registry = TerminalRegistry(path=tmp_path / "terminals.json")
    with pytest.raises(UnknownTerminal):
        registry.register(TerminalRegistrationRequest(id="never-seen"))


def test_register_picks_up_cached_descriptor_after_reload(tmp_path: Path) -> None:
    """A registered terminal can be re-registered (e.g. to update
    nickname) after a manager restart — no need to redo discovery."""
    path = tmp_path / "terminals.json"
    first = TerminalRegistry(path=path)
    first.remember_descriptors([_descriptor()])
    first.register(TerminalRegistrationRequest(id="abc123", kind=TerminalKind.mono_pos))

    second = TerminalRegistry(path=path)
    second.load()
    reg = second.register(TerminalRegistrationRequest(
        id="abc123",
        kind=TerminalKind.mono_pos,
        nickname="Renamed",
    ))
    assert reg.nickname == "Renamed"


def test_unregister_removes_from_file(tmp_path: Path) -> None:
    registry = TerminalRegistry(path=tmp_path / "terminals.json")
    registry.remember_descriptors([_descriptor()])
    registry.register(TerminalRegistrationRequest(id="abc123"))

    registry.unregister("abc123")

    assert registry.all_registrations() == []
    raw = json.loads((tmp_path / "terminals.json").read_text())
    assert raw["terminals"] == []


def test_unregister_unknown_raises(tmp_path: Path) -> None:
    registry = TerminalRegistry(path=tmp_path / "terminals.json")
    with pytest.raises(UnknownTerminal):
        registry.unregister("nope")


def test_load_handles_missing_file(tmp_path: Path) -> None:
    """Fresh install — no terminals.json yet — must not crash."""
    registry = TerminalRegistry(path=tmp_path / "missing.json")
    registry.load()
    assert registry.all_registrations() == []


def test_load_handles_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "terminals.json"
    path.write_text("{ this is not JSON")
    registry = TerminalRegistry(path=path)
    registry.load()
    assert registry.all_registrations() == []  # quietly empty, not crashed


def test_adapter_for_returns_ssi_adapter(tmp_path: Path) -> None:
    """Every TerminalKind we ship today maps to SSITerminalAdapter."""
    registry = TerminalRegistry(path=tmp_path / "terminals.json")
    registry.remember_descriptors([_descriptor()])
    registry.register(TerminalRegistrationRequest(id="abc123", kind=TerminalKind.mono_pos))
    adapter = registry.adapter_for("abc123")
    assert isinstance(adapter, SSITerminalAdapter)
    assert adapter.descriptor.id == "abc123"


def test_adapter_for_unknown_id_raises(tmp_path: Path) -> None:
    registry = TerminalRegistry(path=tmp_path / "terminals.json")
    with pytest.raises(UnknownTerminal):
        registry.adapter_for("nope")


def test_update_merchants_replaces_list(tmp_path: Path) -> None:
    """Settings UI sends the full list — registry replaces, doesn't merge."""
    registry = TerminalRegistry(path=tmp_path / "terminals.json")
    registry.remember_descriptors([_descriptor()])
    registry.register(TerminalRegistrationRequest(id="abc123"))

    reg = registry.update_merchants("abc123", [
        MerchantBinding(merchant_id="M1", terminal_id="T1", nickname="Бар"),
    ])
    assert len(reg.merchants) == 1
    assert reg.merchants[0].nickname == "Бар"

    # Reload from disk — nicknames persist.
    reload = TerminalRegistry(path=tmp_path / "terminals.json")
    reload.load()
    assert reload.get_registration("abc123").merchants[0].nickname == "Бар"


def test_merge_merchant_list_preserves_nicknames(tmp_path: Path) -> None:
    """Bank-side roster changes; operator-set nicknames stick to their
    merchant_id+terminal_id pair across refreshes."""
    registry = TerminalRegistry(path=tmp_path / "terminals.json")
    registry.remember_descriptors([_descriptor()])
    registry.register(TerminalRegistrationRequest(id="abc123"))
    registry.update_merchants("abc123", [
        MerchantBinding(merchant_id="M1", terminal_id="T1", nickname="Бар"),
        MerchantBinding(merchant_id="M2", terminal_id="T1", nickname="Тераса"),
    ])

    # SSI now reports M1 (unchanged) + new M3; M2 is gone.
    merged = registry.merge_merchant_list("abc123", [
        MerchantBinding(merchant_id="M1", terminal_id="T1", merchant_name="ФОП Л"),
        MerchantBinding(merchant_id="M3", terminal_id="T1", merchant_name="ФОП Нове"),
    ])

    by_id = {m.merchant_id: m for m in merged}
    assert by_id["M1"].nickname == "Бар"           # kept
    assert by_id["M1"].merchant_name == "ФОП Л"    # refreshed bank-side name
    assert by_id["M3"].nickname is None             # new — no nickname
    assert "M2" not in by_id                        # removed bank-side


def test_merge_merchant_list_for_unregistered_returns_fresh(tmp_path: Path) -> None:
    """Calling on an unregistered terminal must not raise — returns
    whatever the live SSI list gave us (used during discover/register
    preview flow)."""
    registry = TerminalRegistry(path=tmp_path / "terminals.json")
    fresh = [MerchantBinding(merchant_id="M1", merchant_name="X")]
    out = registry.merge_merchant_list("ghost", fresh)
    assert out == fresh


def test_register_with_initial_merchants(tmp_path: Path) -> None:
    """Single-merchant terminals — UI never shows the editor and just
    pushes the nickname through the initial /register call."""
    registry = TerminalRegistry(path=tmp_path / "terminals.json")
    registry.remember_descriptors([_descriptor()])
    reg = registry.register(TerminalRegistrationRequest(
        id="abc123",
        merchants=[MerchantBinding(merchant_id="M1", nickname="Каса")],
    ))
    assert reg.merchants[0].nickname == "Каса"


def test_re_register_preserves_merchants_when_omitted(tmp_path: Path) -> None:
    """Re-registering to change nickname/default must not blow away
    the merchant list silently."""
    registry = TerminalRegistry(path=tmp_path / "terminals.json")
    registry.remember_descriptors([_descriptor()])
    registry.register(TerminalRegistrationRequest(
        id="abc123",
        merchants=[MerchantBinding(merchant_id="M1", nickname="Бар")],
    ))
    reg = registry.register(TerminalRegistrationRequest(
        id="abc123", nickname="Каса 1",  # only changing terminal nickname
    ))
    assert reg.nickname == "Каса 1"
    assert reg.merchants[0].nickname == "Бар"  # untouched


def test_first_returns_only_registered_terminal(tmp_path: Path) -> None:
    """Operators with a single terminal don't need to pass terminal_id
    — the route uses `first()` as a sensible default."""
    registry = TerminalRegistry(path=tmp_path / "terminals.json")
    assert registry.first() is None  # empty
    registry.remember_descriptors([_descriptor()])
    registry.register(TerminalRegistrationRequest(id="abc123"))
    reg = registry.first()
    assert reg is not None
    assert reg.descriptor.id == "abc123"
