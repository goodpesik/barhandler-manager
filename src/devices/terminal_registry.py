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
from typing import Dict, Type

from src.models.terminal import (
    MerchantBinding,
    TerminalDescriptor,
    TerminalKind,
    TerminalRegistration,
    TerminalRegistrationRequest,
)
from src.services.terminals.base import TerminalAdapter
from src.services.terminals.privatbank import PrivatBankTerminalAdapter
from src.services.terminals.ssi import SSITerminalAdapter

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("terminals.json")


class UnknownTerminal(Exception):
    """Raised when a route asks for a terminal id we don't know."""


# Adapter selection is per-bank because wire formats diverge:
#   - SSI ECR JSON (TCP 3000, STX+LEN+LRC framing) — Mono, Raif,
#     Pivdenny, generic_ssi. PrivatBank also licenses SSI's middleware
#     on some legacy units, but PB merchants moving to JSON should use
#     the privat_pos adapter below; "Privat over SSI" stays available
#     via generic_ssi if anyone needs it.
#   - PrivatBank ECR JSON (TCP 2000, 0x00-terminated framing) — modern
#     Ingenico/PAX/NEWLAND with JSON firmware. Different wire format,
#     own param vocabulary (decimal-comma amount, int step).
# Adding a new vendor with a different wire format = new class + an
# entry here; route layer is unaffected.
_ADAPTER_FOR_KIND: Dict[TerminalKind, Type[TerminalAdapter]] = {
    TerminalKind.mono_pos: SSITerminalAdapter,
    TerminalKind.privat_pos: PrivatBankTerminalAdapter,
    TerminalKind.raif_pos: SSITerminalAdapter,
    TerminalKind.pivdenny_pos: SSITerminalAdapter,
    TerminalKind.generic_ssi: SSITerminalAdapter,
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
        reg = self._registrations.get(terminal_id)
        if reg is None:
            raise UnknownTerminal(terminal_id)
        return reg

    def all_registrations(self) -> list[TerminalRegistration]:
        return list(self._registrations.values())

    def first(self) -> TerminalRegistration | None:
        """Convenience for routes that take terminal_id as optional —
        falls back to the only registered terminal."""
        for reg in self._registrations.values():
            return reg
        return None

    # ---------- mutations ----------

    def register(self, req: TerminalRegistrationRequest) -> TerminalRegistration:
        descriptor = self._last_discovery.get(req.id)
        existing = self._registrations.get(req.id)
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
        if terminal_id not in self._registrations:
            raise UnknownTerminal(terminal_id)
        self._registrations.pop(terminal_id)
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
