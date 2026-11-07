"""Persistent POS terminal registry.

Parallels `PrinterRegistry` — same shape, separate file (`terminals.json`
next to `printers.json`). Concrete adapter is instantiated lazily on
each access; SSI is request/response per call so we don't hold sockets
between charges. The mapping `TerminalKind → TerminalAdapter class` is
maintained here so the route layer never imports a specific vendor.
"""

from __future__ import annotations

import codecs
import json
import locale
import logging
import os
import shutil
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


def _station_encoding() -> str:
    """The code page a manager older than PET-1304 wrote this file in.

    Up to v1.0.10 both halves of the registry used the station's own
    encoding, which on a Ukrainian Windows is cp1251 and on a Western
    one cp1252 — the ANSI code page, whatever `read_text()` without an
    encoding picks. It is only ever used to READ such a file; nothing
    is written in it any more.

    On Windows that is the ANSI code page itself (`mbcs`), not the
    locale setting of today's interpreter: the file was written by a
    build that had no say in the matter, and this one could be started
    in UTF-8 mode, which would leave the old file unreadable and the
    shop with no terminals.
    """
    if os.name == "nt":
        return "mbcs"
    return locale.getpreferredencoding(False) or "utf-8"


def _address_of(reg: TerminalRegistration) -> str:
    net = reg.descriptor.network
    return f"{net.host}:{net.port}" if net else "no address"


def _rekey_winner_first(
    position: int, reg: TerminalRegistration,
) -> tuple[int, int, int, int, int]:
    """Sort key putting the registration that should keep the id first.

    When one terminal is stored under several of its old addresses, one
    of them keeps the id. There is no timestamp in the file to go by, so
    the order is what the shop would lose by picking wrong: the operator's
    own work — the nickname, the named merchant bindings, the default
    merchant. A stale address is NOT part of this on purpose, but it is
    not ignored either: the surviving registration is given the newest
    address of the copies (see `_address_donor`), because nothing in the
    code rewrites a stored address by itself.

    `position` — the place of the entry in terminals.json — is the last
    term, so that two equally-furnished copies still have one answer and
    the same file always gives the same one; dict order was what made
    the loss unpredictable in the first place. Later in the file means
    registered later, which is also the copy whose address is most
    likely to be the live one, so there is nothing left to carry over.
    """
    merchants = reg.merchants or []
    return (
        0 if (reg.nickname or "").strip() else 1,
        -sum(1 for m in merchants if (m.nickname or "").strip()),
        -len(merchants),
        0 if reg.default_merchant_id else 1,
        -position,
    )


def _address_donor(
    candidates: list[tuple[int, str, TerminalRegistration]],
) -> tuple[int, str, TerminalRegistration]:
    """The copy whose address the survivor should end up with.

    The file carries no timestamps, so the only recency signal is the
    order of the entries: a registration is appended when it is written
    under a new id, so a later entry was registered later and its
    address is the one the terminal answered on most recently.

    This matters because nothing else ever fixes it. The address lives
    in `descriptor.network`, and only `register()` — the operator adding
    the terminal by hand — ever replaces a descriptor; neither a
    discovery sweep nor a charge writes it back. So a survivor left
    holding a dead address goes on dialing the dead address, and a
    terminal that worked before the upgrade stops working.
    """
    return max(candidates, key=lambda item: item[0])


def _absorb_losing_registration(
    winner: TerminalRegistration, loser: TerminalRegistration,
) -> tuple[list[str], list[str]]:
    """Take into `winner` what only `loser` has; report what moved and
    what was thrown away.

    Both describe the same physical terminal, so the merchant bindings
    are two readings of one device and can be put together. The winner's
    own values are never overwritten — it was chosen for having them.

    Whatever the winner already has its own answer for cannot be kept,
    and the operator typed it once, so it is named in the log with its
    value. Otherwise a nickname or a default merchant the shop chose
    disappears with nothing left to read it off, not even to put it
    back by hand.
    """
    carried: list[str] = []
    dropped: list[str] = []
    if (loser.nickname or "").strip():
        if not (winner.nickname or "").strip():
            winner.nickname = loser.nickname
            carried.append(f"nickname {loser.nickname!r}")
        elif loser.nickname != winner.nickname:
            dropped.append(
                f"nickname {loser.nickname!r} (the surviving one is "
                f"{winner.nickname!r})",
            )
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
        elif (m.nickname or "").strip() and m.nickname != kept.nickname:
            dropped.append(
                f"merchant {m.merchant_id} nickname {m.nickname!r} (the "
                f"surviving one is {kept.nickname!r})",
            )
    if added:
        carried.append(f"{added} merchant binding(s)")
    if renamed:
        carried.append(f"{renamed} merchant nickname(s)")
    if loser.default_merchant_id:
        if not winner.default_merchant_id:
            winner.default_merchant_id = loser.default_merchant_id
            winner.default_terminal_id = (
                winner.default_terminal_id or loser.default_terminal_id
            )
            carried.append(
                f"default merchant {loser.default_merchant_id}"
                + (
                    f"/{loser.default_terminal_id}"
                    if loser.default_terminal_id else ""
                ),
            )
        elif loser.default_merchant_id != winner.default_merchant_id:
            dropped.append(
                f"default merchant {loser.default_merchant_id} (the "
                f"surviving one is {winner.default_merchant_id})",
            )
    if loser.kind != winner.kind:
        # The kind picks the wire adapter, so it is never taken over on
        # a guess — but two readings of one device disagreeing about its
        # bank is worth saying out loud, with both values.
        dropped.append(
            f"kind {getattr(loser.kind, 'value', loser.kind)} (the "
            f"surviving one speaks "
            f"{getattr(winner.kind, 'value', winner.kind)})",
        )
    return carried, dropped


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
        # Whether the file we read was already UTF-8. A file written by
        # a manager older than PET-1304 is in the station's own code
        # page, and it is rewritten as UTF-8 once — see `_read_text`.
        self._stored_as_utf8 = True

    # ---------- persistence ----------

    def _read_text(self) -> str:
        """The contents of terminals.json, whichever way it was written.

        `save()` writes UTF-8. Up to v1.0.10 both halves used the
        station's own code page, which round-tripped as long as they
        agreed — and they no longer do. Read as ANSI, a UTF-8 «Каса 1»
        comes back as `РљР°СЃР° 1` on a Ukrainian Windows and the next
        save writes the mojibake back for good; on a Western one the
        same bytes raise, the file reads as no terminals at all, and
        the first registration writes over the shop's others.

        So: UTF-8 first, and only a file that is not valid UTF-8 is
        tried in the station's code page — which is what v1.0.10 wrote.
        Cyrillic in a single-byte code page is never valid UTF-8, so
        the two cannot be confused. Such a file is marked for one
        rewrite, after which it is UTF-8 like every other.
        """
        data = self.path.read_bytes()
        try:
            self._stored_as_utf8 = True
            return data.decode("utf-8")
        except UnicodeDecodeError:
            fallback = _station_encoding()
            if codecs.lookup(fallback).name == "utf-8":
                # Nothing else to try — the file is simply broken, and
                # the re-raise has to happen in here while that is still
                # the exception being handled; outside, a bare `raise`
                # is a RuntimeError and the log would name the wrong
                # reason the file could not be read.
                raise
        text = data.decode(fallback)
        self._stored_as_utf8 = False
        logger.info(
            "%s was written by an older manager in this station's code "
            "page (%s) — read as such and rewritten as UTF-8, so the "
            "terminal nicknames stay readable",
            self.path, fallback,
        )
        return text

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self._read_text())
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
            self._index_legacy_network_ids(
                rewrite_file=not self._stored_as_utf8,
            )
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
                "%s not rewritten this start (re-keyed ids, UTF-8 "
                "normalisation): %s — what is stored there keeps working, "
                "the rewrite is tried again on a later start",
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
                self._claim_legacy_id(old_id, stored_id)

    def _index_legacy_network_ids(self, rewrite_file: bool = False) -> None:
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
        `_rekey_winner_first` for which one keeps the id,
        `_absorb_losing_registration` for what is carried out of the
        others and `_address_donor` for whose address the survivor ends
        up with. Nothing is dropped without a line in the log.

        `rewrite_file` saves even when no id moved — used to put a file
        written in the station's own code page back as UTF-8, see
        `_read_text`.
        """
        groups: dict[str, list[tuple[int, str, TerminalRegistration]]] = {}
        derived_aliases: list[tuple[str, str]] = []
        # Where each entry sits in the file. The file has no timestamps,
        # so this is the only recency signal there is: see
        # `_address_donor`.
        positions: dict[str, int] = {}
        for position, (stored_id, reg) in enumerate(self._registrations.items()):
            positions[stored_id] = position
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
            groups.setdefault(current_id, []).append((position, stored_id, reg))
        for legacy_id, current_id in derived_aliases:
            if legacy_id in self._registrations:
                continue
            self._legacy_ids.setdefault(legacy_id, current_id)
        changed = False
        # Sorted so the whole pass is reproducible, not just each group.
        for current_id, group in sorted(groups.items()):
            group.sort(key=lambda item: _rekey_winner_first(item[0], item[2]))
            incumbent = self._registrations.get(current_id)
            candidates = list(group)
            if incumbent is not None:
                # Already stored under the id we compute now: it is the
                # one discovery and this code agree on, so it keeps the
                # id and every legacy copy gives way to it.
                winner = incumbent
                losers = group
                candidates.append(
                    (positions[current_id], current_id, incumbent),
                )
            else:
                _, winner_id, winner = group[0]
                losers = group[1:]
                self._registrations.pop(winner_id, None)
                winner.descriptor.id = current_id
                self._registrations[current_id] = winner
                self._remember_old_id(winner, winner_id, current_id)
                # And every id this registration had already left behind
                # before today. `_index_stored_known_ids` pointed those
                # at the key just popped, `_resolve` takes a single hop,
                # so left alone they answer nothing at all — a till
                # holding one of them would be told the terminal is not
                # registered. The losing copies below get the same
                # treatment; the survivor was the one left out.
                for older in list(winner.known_ids):
                    self._remember_old_id(winner, older, current_id)
                winner.known_ids = [
                    i for i in winner.known_ids if i != current_id
                ]
                logger.info(
                    "terminal %s (s/n %s) re-keyed to %s: the address is no "
                    "longer part of the id, the old id still resolves",
                    winner_id, winner.descriptor.serial, current_id,
                )
            changed = True
            self._carry_newest_address(current_id, winner, candidates)
            for _, loser_id, loser in losers:
                self._registrations.pop(loser_id, None)
                self._remember_old_id(winner, loser_id, current_id)
                for older in loser.known_ids:
                    self._remember_old_id(winner, older, current_id)
                carried, dropped = _absorb_losing_registration(winner, loser)
                logger.warning(
                    "terminal s/n %s was stored twice, as %s (address %s) "
                    "and as %s (address %s) — the same device under two of "
                    "its addresses. %s keeps the registration and answers "
                    "to both ids; carried over from the other: %s; thrown "
                    "away with it: %s",
                    winner.descriptor.serial,
                    current_id, _address_of(winner),
                    loser_id, _address_of(loser),
                    current_id,
                    ", ".join(carried) if carried else "nothing it lacked",
                    ", ".join(dropped) if dropped else "nothing",
                )
        if not (changed or rewrite_file):
            return
        # One copy of the file as the shop had it, before anything in
        # here replaces it.
        self._backup_once()
        # This is the only write `load()` performs, and it can fail for
        # reasons that have nothing to do with us: a read-only file, an
        # antivirus or a backup agent holding terminals.json open on
        # Windows, a full disk. It is guarded by the caller, and it has
        # to stay that way — see the comment there.
        self.save()

    def _carry_newest_address(
        self,
        current_id: str,
        winner: TerminalRegistration,
        candidates: list[tuple[int, str, TerminalRegistration]],
    ) -> None:
        """Give the surviving registration the newest address of the lot.

        Which copy keeps the registration is decided by the operator's
        own work, and that copy can easily be the one sitting at the
        address the terminal has since left. Nothing in the manager
        rewrites a stored address by itself — the adapters read
        `descriptor.network` on every charge and only `register()` ever
        replaces the descriptor — so the survivor would go on dialing a
        dead address, and a terminal that worked before the upgrade
        would stop working.
        """
        _, _, donor = _address_donor(candidates)
        if donor is winner or donor.descriptor.network is None:
            return
        if donor.descriptor.network == winner.descriptor.network:
            return
        was = _address_of(winner)
        winner.descriptor.network = donor.descriptor.network.model_copy()
        winner.descriptor.label = donor.descriptor.label
        logger.warning(
            "terminal %s (s/n %s) keeps the registration but takes the "
            "address of the copy registered after it: %s instead of %s — "
            "the later entry in the file is the one the terminal answered "
            "on last, and nothing else would ever correct this",
            current_id, winner.descriptor.serial, _address_of(winner), was,
        )

    def _backup_once(self) -> None:
        """Keep one copy of terminals.json as the shop had it.

        Both rewrites this file can get on start-up — the re-keyed ids
        and the UTF-8 normalisation — happen unattended, and leave a
        file an older manager reads differently. One copy of the
        original bytes is the difference between a shop that can be put
        back by hand and a shop re-adding every terminal it has. Only
        the first rewrite takes it: a later one would copy the already
        converted file over the original. Failing to take it must not
        cost the rewrite, let alone the start-up.
        """
        backup = self.path.with_suffix(self.path.suffix + ".bak")
        try:
            if backup.exists():
                return
            shutil.copy2(self.path, backup)
            logger.info(
                "%s kept as it was before the manager rewrote it, in %s",
                self.path, backup,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "no backup of %s taken before rewriting it (%s) — the "
                "rewrite goes ahead, there is just nothing to restore from",
                self.path, exc,
            )

    def _remember_old_id(
        self, reg: TerminalRegistration, old_id: str, current_id: str,
    ) -> None:
        """Keep answering to an id this registration has left behind."""
        if not old_id or old_id == current_id:
            return
        if old_id not in reg.known_ids:
            reg.known_ids.append(old_id)
        self._claim_legacy_id(old_id, current_id)

    def _claim_legacy_id(self, old_id: str, current_id: str) -> None:
        """Point an old id at the registration that answers for it.

        Two registrations can claim the same old id — a file edited by
        hand, or one written while a rewrite was interrupted. Whoever
        came first in the file used to win, so the answer depended on
        the order the file happened to be in and nothing said that a
        choice had been made. The lower of the two ids wins instead, so
        every start of every station gives the same answer, and the
        collision is logged: only a re-registration can really settle
        which device that till is holding an id for.

        Overwriting a pointer to an id that is no longer stored is not
        a collision — that is the re-keying moving a whole chain of old
        ids onto the survivor, and it is the point of the exercise.
        """
        held = self._legacy_ids.get(old_id)
        if held is not None and held != current_id and held in self._registrations:
            settled = min(held, current_id)
            logger.warning(
                "old terminal id %s is claimed by two registrations, %s and "
                "%s — it resolves to %s (the lower id, so every start "
                "answers the same); a till holding %s may be talking to the "
                "wrong one, re-register it from the dashboard",
                old_id, held, current_id, settled, old_id,
            )
            self._legacy_ids[old_id] = settled
            return
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

        The bytes are forced to the disk before the rename, because the
        rename being atomic only says that the file is either the old
        one or the new one — not that the new one has reached the
        platter. Written and renamed inside the cache, a power cut right
        after can leave NTFS holding the new name over a zero-length
        file, which is the very loss the temporary file is here to
        prevent.

        UTF-8 explicitly, and `load()` reads it the same way. Up to
        v1.0.10 both halves used the station's code page; agreeing with
        each other is the whole requirement, and a nickname is Cyrillic
        in every shop we have.
        """
        payload = {
            "terminals": [r.model_dump() for r in self._registrations.values()],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
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
