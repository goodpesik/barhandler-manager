"""Persistent POS terminal registry.

Parallels `PrinterRegistry` — same shape, separate file (`terminals.json`
next to `printers.json`). Concrete adapter is instantiated lazily on
each access; SSI is request/response per call so we don't hold sockets
between charges. The mapping `TerminalKind → TerminalAdapter class` is
maintained here so the route layer never imports a specific vendor.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional, Type

from src.models.terminal import (
    MerchantBinding,
    TerminalDescriptor,
    TerminalKind,
    TerminalNetworkAddress,
    TerminalRegistration,
    TerminalRegistrationRequest,
    TerminalTransport,
    legacy_network_terminal_id,
    make_network_terminal_id,
    make_terminal_id,
)
from src.services.terminals.base import TerminalAdapter
from src.services.terminals.bpos import BposTerminalAdapter
from src.services.terminals.oschad import OschadTerminalAdapter
from src.services.terminals.posapi import PosApiTerminalAdapter
from src.services.terminals.privatbank import PrivatBankTerminalAdapter
from src.services.terminals.ssi import SSITerminalAdapter

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("terminals.json")


class UnknownTerminal(Exception):
    """Raised when a route asks for a terminal id we don't know."""


# Adapter selection is per-bank because wire formats diverge:
#   - SSI ECR JSON (TCP 3000, STX+LEN+LRC) — Mono + any SSI-firmware unit
#     (generic_ssi). PrivatBank licenses SSI middleware on some legacy
#     units; those register as generic_ssi.
#   - PrivatBank ECR JSON (TCP 2000, 0x00-terminated) — Ingenico/PAX/
#     NEWLAND with Privat's JSON firmware.
#   - Printec PosAPI (bridge, newline JSON) — Raiffeisen & PUMB Verifone
#     terminals via the Printec service. Brands map here by default.
#   - BPOS1 / BPOS Light (bridge, length-prefixed JSON) — Bank Pivdenny
#     and Sense Bank (formerly Alfa) Ingenico terminals.
#   - Oschadbank ECR (bridge, STX/ETX JSON).
# Adding a vendor with a new wire format = new adapter class + one entry
# here (+ a discovery probe/port in scan.py); the route layer is unaffected.
_ADAPTER_FOR_KIND: Dict[TerminalKind, Type[TerminalAdapter]] = {
    # SSI
    TerminalKind.mono_pos: SSITerminalAdapter,
    TerminalKind.generic_ssi: SSITerminalAdapter,
    # PrivatBank
    TerminalKind.privat_pos: PrivatBankTerminalAdapter,
    # Printec PosAPI
    TerminalKind.raif_pos: PosApiTerminalAdapter,
    TerminalKind.pumb_pos: PosApiTerminalAdapter,
    TerminalKind.generic_posapi: PosApiTerminalAdapter,
    # BPOS
    TerminalKind.pivdenny_pos: BposTerminalAdapter,
    TerminalKind.sense_pos: BposTerminalAdapter,
    TerminalKind.generic_bpos: BposTerminalAdapter,
    # Oschadbank
    TerminalKind.oschad_pos: OschadTerminalAdapter,
}


class TerminalRegistry:
    """In-memory map of registered terminals, backed by a JSON file.

    Discovery results are cached in `_last_discovery` between
    `/terminal/discover` and `/terminal/register` so the frontend
    doesn't have to round-trip the full descriptor — same trick as
    PrinterRegistry.
    """

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path
        self._registrations: Dict[str, TerminalRegistration] = {}
        self._last_discovery: Dict[str, TerminalDescriptor] = {}
        # Old id the POS may still be holding -> the id we use now.
        # Filled by `load()` when a stored registration predates
        # PET-1304. Lookups accept both so nobody has to re-add a
        # terminal that already works.
        self._legacy_ids: Dict[str, str] = {}

    # ---------- persistence ----------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.warning("terminals.json unreadable: %s", exc)
            return
        for entry in raw.get("terminals", []):
            try:
                reg = TerminalRegistration.model_validate(entry)
            except Exception as exc:  # noqa: BLE001
                logger.warning("skipping bad terminal registration: %s", exc)
                continue
            self._registrations[reg.descriptor.id] = reg
        logger.info("loaded %d registered terminals", len(self._registrations))
        self._index_legacy_network_ids()

    def _index_legacy_network_ids(self) -> None:
        """Make a terminal registered by an older manager keep working.

        Before PET-1304 the host address was hashed into the id even
        when the serial was known, so a terminal that already works on a
        shop's till carries an id this code would no longer compute.
        Left alone it would read as a device nobody ever registered, and
        the operator would have to add it again — the exact complaint
        the id change is fixing.

        Two things happen here, for every stored network terminal that
        has a serial:

        * the old host-based id is derived from the registration's own
          address and kept as an alias, so a till that still asks with
          it is answered. It is derived and not stored, so it goes on
          working after this file has been rewritten with the new id;
        * if the stored id IS that old hash, the registration moves to
          the id we compute now, and the file is written once. From then
          on discovery and the file agree, and a DHCP move no longer
          changes anything.

        A stored id we cannot account for is left exactly as it is.
        """
        moves: list[tuple[str, str, TerminalRegistration]] = []
        for stored_id, reg in self._registrations.items():
            net = reg.descriptor.network
            serial = reg.descriptor.serial
            if net is None or not serial:
                continue
            current_id = make_network_terminal_id(net.host, net.port, serial)
            legacy_id = legacy_network_terminal_id(net.host, net.port, serial)
            if legacy_id != current_id:
                self._legacy_ids[legacy_id] = current_id
            if stored_id == current_id or stored_id != legacy_id:
                continue
            moves.append((stored_id, current_id, reg))
        if not moves:
            return
        for stored_id, current_id, reg in moves:
            self._registrations.pop(stored_id, None)
            reg.descriptor.id = current_id
            self._registrations[current_id] = reg
            logger.info(
                "terminal %s (s/n %s) re-keyed to %s: the address is no "
                "longer part of the id, the old id still resolves",
                stored_id, reg.descriptor.serial, current_id,
            )
        self.save()

    def _resolve(self, terminal_id: str) -> Optional[str]:
        """The id we store this terminal under, accepting an id from
        before the re-keying. None when we know neither."""
        if terminal_id in self._registrations:
            return terminal_id
        current = self._legacy_ids.get(terminal_id)
        if current is not None and current in self._registrations:
            return current
        return None

    def save(self) -> None:
        payload = {
            "terminals": [r.model_dump() for r in self._registrations.values()],
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    # ---------- discovery cache ----------

    def remember_descriptors(self, descriptors: list[TerminalDescriptor]) -> None:
        self._last_discovery = {d.id: d for d in descriptors}

    # ---------- lookups ----------

    def get_registration(self, terminal_id: str) -> TerminalRegistration:
        resolved = self._resolve(terminal_id)
        if resolved is None:
            raise UnknownTerminal(terminal_id)
        return self._registrations[resolved]

    def all_registrations(self) -> list[TerminalRegistration]:
        return list(self._registrations.values())

    def first(self) -> TerminalRegistration | None:
        """Convenience for routes that take terminal_id as optional —
        falls back to the only registered terminal."""
        for reg in self._registrations.values():
            return reg
        return None

    # ---------- mutations ----------

    def add_manual_descriptor(
        self,
        host: str,
        port: int,
        kind: TerminalKind = TerminalKind.generic_ssi,
        label: Optional[str] = None,
    ) -> TerminalDescriptor:
        """Stage a TerminalDescriptor from operator-provided host/port
        without going through `/terminal/discover` — needed when the
        terminal is reachable but on a subnet our scan can't enumerate
        (carrier CGNAT, mobile hotspot with non-standard subnet, VLAN
        across switches). Same shape as discovery — the descriptor goes
        into `_last_discovery` so the existing `register()` path picks
        it up.

        Serial is unknown here — the operator types an address, nobody
        has talked to the device yet — so the id can only be the address
        itself, and it does change if the terminal later moves to a new
        IP. That is the same fallback discovery uses when its probe
        could not read a serial; both go through one helper so the two
        paths cannot drift apart."""
        terminal_id = make_network_terminal_id(host, port, None)
        descriptor = TerminalDescriptor(
            id=terminal_id,
            transport=TerminalTransport.network,
            label=label or f"{kind.value} @ {host}",
            kind=kind,
            network=TerminalNetworkAddress(host=host, port=port),
        )
        self._last_discovery[terminal_id] = descriptor
        return descriptor

    def add_manual_descriptor_serial(
        self,
        port: str,
        baudrate: int = 115200,
        kind: TerminalKind = TerminalKind.privat_pos,
        label: Optional[str] = None,
    ) -> TerminalDescriptor:
        """Stage a serial/COM terminal (USB terminal on Windows = virtual COM
        port). Same JSON protocol as TCP, driven over the port. Caches into
        `_last_discovery` so the existing `register()` path binds it — mirrors
        add_manual_descriptor for network."""
        from src.models.terminal import TerminalSerialAddress

        terminal_id = make_terminal_id(TerminalTransport.serial, port, "")
        descriptor = TerminalDescriptor(
            id=terminal_id,
            transport=TerminalTransport.serial,
            label=label or f"{kind.value} @ {port}",
            kind=kind,
            com=TerminalSerialAddress(port=port, baudrate=baudrate),
        )
        self._last_discovery[terminal_id] = descriptor
        return descriptor

    def register(self, req: TerminalRegistrationRequest) -> TerminalRegistration:
        descriptor = self._last_discovery.get(req.id)
        resolved = self._resolve(req.id)
        existing = self._registrations[resolved] if resolved else None
        if descriptor is None:
            # Allow re-register after a manager restart — operator can
            # then update nickname / default merchant without redoing
            # a full network scan.
            if existing is None:
                raise UnknownTerminal(
                    f"{req.id}: run /terminal/discover first or provide a known id",
                )
            descriptor = existing.descriptor
        # Preserve operator-set nicknames across re-registration: if
        # the caller didn't supply a fresh merchants list, keep the
        # one we already had so nicknames don't get wiped.
        merchants = (
            req.merchants
            if req.merchants is not None
            else (existing.merchants if existing else [])
        )
        reg = TerminalRegistration(
            descriptor=descriptor,
            kind=req.kind,
            nickname=req.nickname,
            default_merchant_id=req.default_merchant_id,
            default_terminal_id=req.default_terminal_id,
            merchants=merchants,
        )
        self._registrations[descriptor.id] = reg
        self.save()
        return reg

    def update_merchants(
        self, terminal_id: str, merchants: list[MerchantBinding],
    ) -> TerminalRegistration:
        """Replace the merchant binding list — used by the Settings UI
        after the operator fills in nicknames."""
        reg = self.get_registration(terminal_id)
        reg.merchants = merchants
        self.save()
        return reg

    def merge_merchant_list(
        self,
        terminal_id: str,
        fresh: list[MerchantBinding],
    ) -> list[MerchantBinding]:
        """Refresh the bank-side merchant list while keeping nicknames.

        Called from `GET /terminal/{id}/merchants` so the SSI roster
        stays current (a new merchant added bank-side appears here)
        but the operator's nicknames survive. Match is by
        merchant_id+terminal_id pair; unmatched stored entries are
        dropped (merchant removed bank-side).
        """
        reg = self._registrations.get(terminal_id)
        nickname_index: dict[tuple[str, str], str] = {}
        if reg is not None:
            for m in reg.merchants:
                key = (m.merchant_id, m.terminal_id or "")
                if m.nickname:
                    nickname_index[key] = m.nickname
        merged: list[MerchantBinding] = []
        for m in fresh:
            key = (m.merchant_id, m.terminal_id or "")
            merged.append(MerchantBinding(
                merchant_id=m.merchant_id,
                terminal_id=m.terminal_id,
                merchant_name=m.merchant_name,
                nickname=nickname_index.get(key) or m.nickname,
            ))
        if reg is not None:
            reg.merchants = merged
            self.save()
        return merged

    def unregister(self, terminal_id: str) -> None:
        resolved = self._resolve(terminal_id)
        if resolved is None:
            raise UnknownTerminal(terminal_id)
        self._registrations.pop(resolved)
        self._legacy_ids = {
            old: new for old, new in self._legacy_ids.items() if new != resolved
        }
        self.save()

    # ---------- adapter access ----------

    def adapter_for(self, terminal_id: str) -> TerminalAdapter:
        """Instantiate the right adapter for a registered terminal.

        Adapters are stateless beyond the registration, so we make a
        fresh instance per call — keeps the hot path simple and avoids
        any held-socket invalidation when terminals reboot / DHCP-shift.
        """
        registration = self.get_registration(terminal_id)
        kind_enum = (
            registration.kind
            if isinstance(registration.kind, TerminalKind)
            else TerminalKind(registration.kind)
        )
        adapter_cls = _ADAPTER_FOR_KIND.get(kind_enum, SSITerminalAdapter)
        return adapter_cls(registration)
