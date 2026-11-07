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
    legacy_network_terminal_id,
    make_network_terminal_id,
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


# ---------------------------------------------------------------------------
# PET-1304 — the id of a network terminal
# ---------------------------------------------------------------------------


def test_one_serial_keeps_one_id_across_a_dhcp_move() -> None:
    """A serial identifies the device, so a new lease must not mint a new
    id — and two devices must never share one."""
    at_first_lease = make_network_terminal_id("192.168.1.100", 3000, "V1E0207420")
    at_second_lease = make_network_terminal_id("192.168.1.109", 3000, "V1E0207420")
    assert at_first_lease == at_second_lease

    other_device = make_network_terminal_id("192.168.1.100", 3000, "V1E0207421")
    assert other_device != at_first_lease


def test_without_a_serial_the_id_still_follows_the_address() -> None:
    """Nothing better exists before a probe reads the serial, so the
    address is the identity there, as it always was."""
    here = make_network_terminal_id("192.168.1.100", 3000, None)
    there = make_network_terminal_id("192.168.1.109", 3000, None)
    assert here != there


def test_terminal_registered_before_the_rekeying_is_still_found(tmp_path: Path) -> None:
    """The upgrade: a terminal added by an older manager is stored under
    the old host-based id. After loading it must answer to that id (the
    till kept it) and to the id discovery computes now, without anyone
    re-adding the terminal."""
    path = tmp_path / "terminals.json"
    host, port, serial = "192.168.1.109", 3000, "V1E0207420"
    legacy_id = legacy_network_terminal_id(host, port, serial)
    current_id = make_network_terminal_id(host, port, serial)
    assert legacy_id != current_id  # otherwise this test proves nothing

    descriptor = _descriptor(host=host, port=port, sid=legacy_id)
    path.write_text(json.dumps({"terminals": [{
        "descriptor": descriptor.model_dump(),
        "kind": "mono_pos",
        "nickname": "Бар 1",
    }]}))

    registry = TerminalRegistry(path=path)
    registry.load()

    assert registry.get_registration(legacy_id).nickname == "Бар 1"
    assert registry.get_registration(current_id).nickname == "Бар 1"
    assert [r.descriptor.id for r in registry.all_registrations()] == [current_id]

    # And after the rewritten file is loaded again — the id the till
    # holds still resolves, because the old id is derived, not stored.
    reloaded = TerminalRegistry(path=path)
    reloaded.load()
    assert reloaded.get_registration(legacy_id).nickname == "Бар 1"
    assert reloaded.get_registration(current_id).nickname == "Бар 1"


# ---------------------------------------------------------------------------
# PET-1304 — collapsing the old ids onto one, without losing a registration
# ---------------------------------------------------------------------------


_SERIAL = "V1E0207420"


def _stored_under_its_legacy_id(host: str, **fields) -> dict:
    """One terminals.json entry as an older manager wrote it: keyed by
    the hash of the address it had at the time."""
    descriptor = _descriptor(
        host=host, sid=legacy_network_terminal_id(host, 3000, _SERIAL),
    )
    entry = {"descriptor": descriptor.model_dump(), "kind": "mono_pos"}
    entry.update(fields)
    return entry


def test_one_terminal_stored_under_two_addresses_loses_nothing(
    tmp_path: Path,
) -> None:
    """The same device is in the file twice, once per address it has
    held, and both collapse onto one id. Overwriting one with the other
    threw away a nickname and merchant bindings nobody could get back."""
    path = tmp_path / "terminals.json"
    path.write_text(json.dumps({"terminals": [
        _stored_under_its_legacy_id(
            "10.0.0.5",
            nickname="Бар 1",
            default_merchant_id="000000060007176",
            merchants=[MerchantBinding(
                merchant_id="60007176", nickname="Каса",
            ).model_dump()],
        ),
        _stored_under_its_legacy_id(
            "10.0.0.9",
            merchants=[MerchantBinding(merchant_id="60007999").model_dump()],
        ),
    ]}))
    first_id = legacy_network_terminal_id("10.0.0.5", 3000, _SERIAL)
    second_id = legacy_network_terminal_id("10.0.0.9", 3000, _SERIAL)
    current_id = make_network_terminal_id("10.0.0.5", 3000, _SERIAL)

    registry = TerminalRegistry(path=path)
    registry.load()

    # One registration left, under the id this code computes now.
    assert [r.descriptor.id for r in registry.all_registrations()] == [current_id]
    # The furnished copy keeps the id — the operator's own work is what
    # cannot be recovered, a stale address is rewritten by the next scan.
    winner = registry.get_registration(current_id)
    assert winner.nickname == "Бар 1"
    assert winner.default_merchant_id == "000000060007176"
    assert winner.descriptor.network.host == "10.0.0.5", (
        "the furnished copy keeps the registration, including its address"
    )
    # And the one that gave way is not gone: a till holding either old id
    # is still answered, and the merchant bindings of both are kept.
    assert registry.get_registration(first_id).nickname == "Бар 1"
    assert registry.get_registration(second_id).nickname == "Бар 1"
    assert sorted(m.merchant_id for m in winner.merchants) == [
        "60007176", "60007999",
    ]


def test_a_registration_already_on_the_current_id_is_not_overwritten(
    tmp_path: Path,
) -> None:
    """The worst case of the same collision: one copy is already stored
    under the id we compute now (it has been through this once), and a
    legacy copy of the same device would land on top of it."""
    path = tmp_path / "terminals.json"
    current_id = make_network_terminal_id("10.0.0.9", 3000, _SERIAL)
    already_current = _descriptor(host="10.0.0.9", sid=current_id)
    path.write_text(json.dumps({"terminals": [
        {
            "descriptor": already_current.model_dump(),
            "kind": "mono_pos",
            "nickname": "Бар 1",
            "default_merchant_id": "000000060007176",
        },
        _stored_under_its_legacy_id("10.0.0.5", nickname="Стара адреса"),
    ]}))
    legacy_id = legacy_network_terminal_id("10.0.0.5", 3000, _SERIAL)

    registry = TerminalRegistry(path=path)
    registry.load()

    assert [r.descriptor.id for r in registry.all_registrations()] == [current_id]
    kept = registry.get_registration(current_id)
    assert kept.nickname == "Бар 1"
    assert kept.default_merchant_id == "000000060007176"
    assert registry.get_registration(legacy_id).nickname == "Бар 1"


def test_a_failed_save_during_load_does_not_stop_the_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`load()` runs inside the server's lifespan, and the re-keying
    writes the file. A read-only file or an antivirus holding it open
    must cost the rewrite, not the whole start-up."""
    path = tmp_path / "terminals.json"
    path.write_text(json.dumps({"terminals": [
        _stored_under_its_legacy_id("10.0.0.5", nickname="Бар 1"),
    ]}))
    legacy_id = legacy_network_terminal_id("10.0.0.5", 3000, _SERIAL)
    current_id = make_network_terminal_id("10.0.0.5", 3000, _SERIAL)

    registry = TerminalRegistry(path=path)

    def refuses_to_write() -> None:
        raise PermissionError(path)

    monkeypatch.setattr(registry, "save", refuses_to_write)
    registry.load()

    # Started, and usable by both ids for the whole of this run.
    assert registry.get_registration(legacy_id).nickname == "Бар 1"
    assert registry.get_registration(current_id).nickname == "Бар 1"


def test_the_id_the_pos_holds_survives_an_address_change(tmp_path: Path) -> None:
    """The sequence this ticket exists for. A shop upgrades while the
    terminal is at one address, its lease then moves, and it is
    re-registered at the new one. The id the POS has held all along has
    to keep working after the next restart — the old id derived from the
    stored address stops being derivable the moment that address
    changes."""
    path = tmp_path / "terminals.json"
    path.write_text(json.dumps({"terminals": [
        _stored_under_its_legacy_id("10.0.0.5", nickname="Бар 1"),
    ]}))
    pos_id = legacy_network_terminal_id("10.0.0.5", 3000, _SERIAL)
    current_id = make_network_terminal_id("10.0.0.5", 3000, _SERIAL)

    registry = TerminalRegistry(path=path)
    registry.load()

    # The lease moves; the scan reports the same device at the new
    # address, and the operator re-registers with the id they have.
    registry.remember_descriptors([_descriptor(host="10.0.0.99", sid=current_id)])
    reg = registry.register(TerminalRegistrationRequest(
        id=pos_id, kind=TerminalKind.mono_pos, nickname="Бар 1",
    ))
    assert reg.descriptor.network.host == "10.0.0.99", (
        "re-registering through the old id must take the freshly "
        "discovered address, not the stored one"
    )

    restarted = TerminalRegistry(path=path)
    restarted.load()
    assert restarted.get_registration(pos_id).descriptor.id == current_id
    assert restarted.get_registration(pos_id).descriptor.network.host == "10.0.0.99"


def test_merchant_refresh_through_an_old_id_is_stored(tmp_path: Path) -> None:
    """`GET /terminal/{id}/merchants` refreshes the bank-side roster and
    keeps it. Asked with the id the POS holds, the list was answered but
    never written down, so the registration stayed empty."""
    path = tmp_path / "terminals.json"
    path.write_text(json.dumps({"terminals": [
        _stored_under_its_legacy_id("10.0.0.5", nickname="Бар 1"),
    ]}))
    pos_id = legacy_network_terminal_id("10.0.0.5", 3000, _SERIAL)

    registry = TerminalRegistry(path=path)
    registry.load()

    merged = registry.merge_merchant_list(pos_id, [
        MerchantBinding(merchant_id="60007176", merchant_name="ФОП Один"),
    ])

    assert [m.merchant_id for m in merged] == ["60007176"]
    stored = registry.get_registration(pos_id)
    assert [m.merchant_id for m in stored.merchants] == ["60007176"]
    reloaded = TerminalRegistry(path=path)
    reloaded.load()
    assert [m.merchant_id for m in reloaded.get_registration(pos_id).merchants] == [
        "60007176",
    ]
