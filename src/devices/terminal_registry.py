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
import os
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


def _address_of(reg: TerminalRegistration) -> str:
    net = reg.descriptor.network
    return f"{net.host}:{net.port}" if net else "no address"


def _rekey_winner_first(
    stored_id: str, reg: TerminalRegistration,
) -> tuple[int, int, int, int, str]:
    """Sort key putting the registration that should keep the id first.

    When one terminal is stored under several of its old addresses, one
    of them keeps the id. There is no timestamp in the file to go by, so
    the order is what the shop would lose by picking wrong: the operator's
    own work — the nickname, the named merchant bindings, the default
    merchant. A stale address is NOT part of this on purpose: the next
    discovery or charge rewrites the address anyway, while a nickname
    nobody typed again is gone for good.

    The stored id is the last term so that two equally-furnished copies
    still have one answer, and the same file always gives the same one —
    dict order was what made the loss unpredictable in the first place.
    """
    merchants = reg.merchants or []
    return (
        0 if (reg.nickname or "").strip() else 1,
        -sum(1 for m in merchants if (m.nickname or "").strip()),
        -len(merchants),
        0 if reg.default_merchant_id else 1,
        stored_id,
    )


def _absorb_losing_registration(
    winner: TerminalRegistration, loser: TerminalRegistration,
) -> list[str]:
    """Take into `winner` what only `loser` has; report what moved.

    Both describe the same physical terminal, so the merchant bindings
    are two readings of one device and can be put together. The winner's
    own values are never overwritten — it was chosen for having them.
    """
    carried: list[str] = []
    if not (winner.nickname or "").strip() and (loser.nickname or "").strip():
        winner.nickname = loser.nickname
        carried.append(f"nickname {loser.nickname!r}")
    by_key = {
        (m.merchant_id, m.terminal_id or ""): m for m in winner.merchants
    }
    added = 0
    renamed = 0
    for m in loser.merchants:
        key = (m.merchant_id, m.terminal_id or "")
        kept = by_key.get(key)
        if kept is None:
            winner.merchants.append(m)
            by_key[key] = m
            added += 1
        elif not (kept.nickname or "").strip() and (m.nickname or "").strip():
            kept.nickname = m.nickname
            renamed += 1
    if added:
        carried.append(f"{added} merchant binding(s)")
    if renamed:
        carried.append(f"{renamed} merchant nickname(s)")
    if not winner.default_merchant_id and loser.default_merchant_id:
        winner.default_merchant_id = loser.default_merchant_id
        winner.default_terminal_id = (
            winner.default_terminal_id or loser.default_terminal_id
        )
        carried.append("default merchant")
    return carried


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
        self._index_stored_known_ids()
        try:
            self._index_legacy_network_ids()
        except Exception as exc:  # noqa: BLE001
            # The re-keying rewrites terminals.json, and `load()` runs
            # inside the server's lifespan, before the work journal and
            # the station settings are loaded. A failed write — read-only
            # file, antivirus holding it open, full disk — would stop the
            # manager from starting at all, which is far worse than one
            # terminal keeping its old id for one more run. Both ids
            # resolve in memory either way, and the next start tries the
            # rewrite again.
            logger.warning(
                "terminal ids not re-keyed in %s this start: %s — the ids "
                "stored there keep working, the rewrite is tried again on "
                "a later start",
                self.path, exc,
            )

    def _index_stored_known_ids(self) -> None:
        """Accept the ids a registration carries from its own past.

        Written down by whoever moved the registration (see
        `known_ids`), because the alias derived below only holds while
        the stored address is still the one the old id was hashed from.
        A live registration key is never shadowed by someone's alias.
        """
        for stored_id, reg in self._registrations.items():
            for old_id in reg.known_ids:
                if old_id == stored_id or old_id in self._registrations:
                    continue
                self._legacy_ids.setdefault(old_id, stored_id)

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

        One physical terminal can be stored two or three times over, once
        per address it has held, and all of those collapse onto one id
        here. They are therefore merged, never overwritten: see
        `_rekey_winner_first` for which one keeps the id and
        `_absorb_losing_registration` for what is carried out of the
        others. Nothing is dropped without a line in the log.
        """
        groups: dict[str, list[tuple[str, TerminalRegistration]]] = {}
        derived_aliases: list[tuple[str, str]] = []
        for stored_id, reg in self._registrations.items():
            net = reg.descriptor.network
            serial = reg.descriptor.serial
            if net is None or not serial:
                continue
            current_id = make_network_terminal_id(net.host, net.port, serial)
            legacy_id = legacy_network_terminal_id(net.host, net.port, serial)
            if legacy_id != current_id:
                derived_aliases.append((legacy_id, current_id))
            if stored_id == current_id or stored_id != legacy_id:
                continue
            groups.setdefault(current_id, []).append((stored_id, reg))
        for legacy_id, current_id in derived_aliases:
            if legacy_id in self._registrations:
                continue
            self._legacy_ids.setdefault(legacy_id, current_id)
        changed = False
        # Sorted so the whole pass is reproducible, not just each group.
        for current_id, group in sorted(groups.items()):
            group.sort(key=lambda item: _rekey_winner_first(*item))
            incumbent = self._registrations.get(current_id)
            if incumbent is not None:
                # Already stored under the id we compute now: it is the
                # one discovery and this code agree on, so it keeps the
                # id and every legacy copy gives way to it.
                winner = incumbent
                losers = group
            else:
                winner_id, winner = group[0]
                losers = group[1:]
                self._registrations.pop(winner_id, None)
                winner.descriptor.id = current_id
                self._registrations[current_id] = winner
                self._remember_old_id(winner, winner_id, current_id)
                logger.info(
                    "terminal %s (s/n %s) re-keyed to %s: the address is no "
                    "longer part of the id, the old id still resolves",
                    winner_id, winner.descriptor.serial, current_id,
                )
            changed = True
            for loser_id, loser in losers:
                self._registrations.pop(loser_id, None)
                self._remember_old_id(winner, loser_id, current_id)
                for older in loser.known_ids:
                    self._remember_old_id(winner, older, current_id)
                carried = _absorb_losing_registration(winner, loser)
                logger.warning(
                    "terminal s/n %s was stored twice, as %s (address %s) "
                    "and as %s (address %s) — the same device under two of "
                    "its addresses. %s keeps the registration and answers "
                    "to both ids; carried over from the other: %s",
                    winner.descriptor.serial,
                    current_id, _address_of(winner),
                    loser_id, _address_of(loser),
                    current_id,
                    ", ".join(carried) if carried else "nothing it lacked",
                )
        if not changed:
            return
        # This is the only write `load()` performs, and it can fail for
        # reasons that have nothing to do with us: a read-only file, an
        # antivirus or a backup agent holding terminals.json open on
        # Windows, a full disk. It is guarded by the caller, and it has
        # to stay that way — see the comment there.
        self.save()

    def _remember_old_id(
        self, reg: TerminalRegistration, old_id: str, current_id: str,
    ) -> None:
        """Keep answering to an id this registration has left behind."""
        if not old_id or old_id == current_id:
            return
        if old_id not in reg.known_ids:
            reg.known_ids.append(old_id)
        self._legacy_ids[old_id] = current_id

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
        """Replace terminals.json in one step.

        Writing over the file in place truncates it first, so a power cut
        or a kill in the middle left a half-written file behind — and
        that reads as zero registrations on the next start, after which
        the first `register()` writes the shop's only terminal over the
        remains of all of them. The temporary file is in the same folder
        so the rename stays on one filesystem and is therefore atomic.
        Same shape as `station_settings` and `offline_offer` already use.
        """
        payload = {
            "terminals": [r.model_dump() for r in self._registrations.values()],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

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
        resolved = self._resolve(req.id)
        existing = self._registrations[resolved] if resolved else None
        descriptor = self._last_discovery.get(req.id)
        if descriptor is None and resolved is not None and resolved != req.id:
            # The caller asked with an id this terminal used to have, so
            # the scan filed its fresh descriptor under the id we use
            # now. Without this the stored descriptor is reused and the
            # address just discovered — the whole point of the scan — is
            # thrown away, leaving the adapter aimed at the old one.
            descriptor = self._last_discovery.get(resolved)
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
        known_ids = list(existing.known_ids) if existing else []
        if resolved is not None and resolved != descriptor.id:
            # Re-registering moved this terminal to another id: drop the
            # old key instead of leaving a second copy behind, and keep
            # answering to it.
            self._registrations.pop(resolved, None)
            if resolved not in known_ids:
                known_ids.append(resolved)
            self._legacy_ids = {
                old: (descriptor.id if new == resolved else new)
                for old, new in self._legacy_ids.items()
            }
            self._legacy_ids[resolved] = descriptor.id
        known_ids = [i for i in known_ids if i != descriptor.id]
        reg = TerminalRegistration(
            descriptor=descriptor,
            kind=req.kind,
            nickname=req.nickname,
            default_merchant_id=req.default_merchant_id,
            default_terminal_id=req.default_terminal_id,
            merchants=merchants,
            known_ids=known_ids,
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

        The lookup goes through `_resolve`, like every other one: asked
        with an id the terminal used to have, a direct dict read found
        nothing, so the merged list was answered to the POS but never
        stored — the refresh was silently thrown away.
        """
        resolved = self._resolve(terminal_id)
        reg = self._registrations[resolved] if resolved else None
        if reg is None:
            logger.info(
                "merchant list of %s refreshed but not stored: no such "
                "terminal registered (discover/register not finished yet)",
                terminal_id,
            )
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
