"""Persistent printer registry.

Holds the operator's role assignments in `printers.json` (next to
`config.yaml`) and opens / caches the physical connections on demand.
Each registered printer is wrapped in a `PrinterDevice` and kept warm
in memory; we close everything on shutdown.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

from src.devices.printer import PrinterDevice
from src.devices.scan import discover_all
from src.models.printer import (
    PrintProtocol,
    PrinterDescriptor,
    PrinterKind,
    PrinterRegistration,
    PrinterTransport,
    RegistrationRequest,
)

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("printers.json")


class UnknownPrinter(Exception):
    """Raised when a route asks for a printer id we don't know."""


class PrinterRegistry:
    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path
        self._registrations: Dict[str, PrinterRegistration] = {}
        # PrinterDevice instances are reused across prints.
        self._devices: Dict[str, PrinterDevice] = {}
        # Discoveries cached in-memory between a /discover and a /register
        # call so the frontend doesn't have to round-trip the full descriptor.
        self._last_discovery: Dict[str, PrinterDescriptor] = {}

    # ---------- persistence ----------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except Exception as exc:
            logger.warning("printers.json unreadable: %s", exc)
            return
        upgraded = False
        for entry in raw.get("printers", []):
            try:
                reg = PrinterRegistration.model_validate(entry)
            except Exception as exc:
                logger.warning("skipping bad registration: %s", exc)
                continue
            # Auto-upgrade: a label-kind printer registered before
            # PrintProtocol existed loads with protocol=escpos (default),
            # but XP-246B & friends ship in TSPL mode — keep them
            # working without forcing the operator to re-register.
            kind_value = reg.kind.value if hasattr(reg.kind, "value") else reg.kind
            had_protocol = "protocol" in entry
            if not had_protocol and kind_value == "label":
                reg.protocol = PrintProtocol.tspl
                upgraded = True
            self._registrations[reg.descriptor.id] = reg
        if upgraded:
            self.save()
        logger.info("loaded %d registered printers", len(self._registrations))

    def save(self) -> None:
        payload = {"printers": [r.model_dump() for r in self._registrations.values()]}
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    # ---------- discovery ----------

    def discover(self) -> list[PrinterDescriptor]:
        descriptors = discover_all()
        self._last_discovery = {d.id: d for d in descriptors}
        return descriptors

    def get_registration(self, printer_id: str) -> PrinterRegistration:
        reg = self._registrations.get(printer_id)
        if reg is None:
            raise UnknownPrinter(printer_id)
        return reg

    def all_registrations(self) -> list[PrinterRegistration]:
        return list(self._registrations.values())

    def for_kind(self, kind: PrinterKind) -> Optional[PrinterRegistration]:
        """Pick the first registration matching `kind` — used as the
        default for /print/receipt when no explicit printer_id is given."""
        kind_value = kind.value if isinstance(kind, PrinterKind) else kind
        for reg in self._registrations.values():
            reg_kind = reg.kind.value if isinstance(reg.kind, PrinterKind) else reg.kind
            if reg_kind == kind_value:
                return reg
        return None

    # ---------- mutations ----------

    def register(self, req: RegistrationRequest) -> PrinterRegistration:
        descriptor = self._last_discovery.get(req.id)
        if descriptor is None:
            # Allow re-registering a printer we've seen before without a
            # fresh discovery (e.g. on a manager restart).
            existing = self._registrations.get(req.id)
            if existing is None:
                raise UnknownPrinter(
                    f"{req.id}: run /devices/discover first or provide a known id"
                )
            descriptor = existing.descriptor
        # PrintProtocol auto-default: dedicated label printers default
        # to TSPL (they ship in `Print mode: LABEL` and silently ignore
        # ESC/POS) — everything else defaults to ESC/POS. The operator
        # can override either way via the request.
        if req.protocol is not None:
            protocol = req.protocol
        else:
            protocol = (
                PrintProtocol.tspl
                if req.kind == PrinterKind.label
                else PrintProtocol.escpos
            )
        kwargs = dict(
            descriptor=descriptor,
            kind=req.kind,
            nickname=req.nickname,
            paper_width=req.paper_width,
            render_mode=req.render_mode,
            code_page=req.code_page,
            drawer_pin=req.drawer_pin,
            protocol=protocol,
        )
        if req.label_height is not None:
            kwargs["label_height"] = req.label_height
        if req.label_gap is not None:
            kwargs["label_gap"] = req.label_gap
        reg = PrinterRegistration(**kwargs)
        self._registrations[descriptor.id] = reg
        self.save()
        return reg

    def unregister(self, printer_id: str) -> None:
        if printer_id not in self._registrations:
            raise UnknownPrinter(printer_id)
        self._registrations.pop(printer_id)
        device = self._devices.pop(printer_id, None)
        if device is not None:
            # Fire-and-forget — disconnect runs in the worker task we own,
            # we never raise inside disconnect.
            import asyncio
            asyncio.create_task(device.disconnect())
        self.save()

    # ---------- device access ----------

    async def get_device(self, printer_id: str) -> PrinterDevice:
        device = self._devices.get(printer_id)
        if device is not None:
            return device
        reg = self.get_registration(printer_id)
        device = self._build_device(reg)
        if reg.descriptor.transport == PrinterTransport.usb.value:
            await device.connect()
        elif reg.descriptor.transport == PrinterTransport.network.value:
            await device.connect()
        # bluetooth: Phase 2 — device stays disconnected
        self._devices[printer_id] = device
        return device

    async def disconnect_all(self) -> None:
        for device in list(self._devices.values()):
            await device.disconnect()
        self._devices.clear()

    # ---------- helpers ----------

    @staticmethod
    def _build_device(reg: PrinterRegistration) -> PrinterDevice:
        descriptor = reg.descriptor
        cfg: dict = {
            "enabled": True,
            "paper_width": reg.paper_width,
            "render_mode": reg.render_mode,
            "code_page": reg.code_page,
            "drawer_pin": reg.drawer_pin,
        }
        transport = descriptor.transport
        if isinstance(transport, PrinterTransport):
            transport = transport.value
        if transport == "usb":
            usb = descriptor.usb
            cfg.update({
                "connection": "usb",
                "vendor_id": usb.vendor_id,
                "product_id": usb.product_id,
                "in_ep": usb.in_ep,
                "out_ep": usb.out_ep,
            })
        elif transport == "network":
            net = descriptor.network
            cfg.update({"connection": "network", "host": net.host, "port": net.port})
        elif transport == "bluetooth":
            raise NotImplementedError("bluetooth transport is Phase 2")
        return PrinterDevice(descriptor.id, cfg)
