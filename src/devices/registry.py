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
from src.devices.printer_models import protocol_for_model
from src.models.printer import (
    NetworkAddress,
    PrintProtocol,
    PrinterDescriptor,
    PrinterKind,
    PrinterRegistration,
    PrinterTransport,
    RegistrationRequest,
    UsbAddress,
    make_id,
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
        # Задачі роз'єднання, які ще не добігли — тримаємо посилання, див.
        # `_drop_cached_device`.
        self._pending_disconnects: set = set()
        self._draining: set = set()
        # Discoveries cached in-memory between a /discover and a /register
        # call so the frontend doesn't have to round-trip the full descriptor.
        self._last_discovery: Dict[str, PrinterDescriptor] = {}
        # Entries of printers.json this version cannot parse. Kept verbatim so
        # every save() puts them back: nobody asked us to delete a registration
        # just because we could not read it (BH-178).
        self._unreadable: list = []

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
        self._unreadable = []
        for entry in raw.get("printers", []):
            try:
                reg = PrinterRegistration.model_validate(entry)
            except Exception as exc:
                logger.warning("skipping bad registration: %s", exc)
                self._unreadable.append(entry)
                continue
            kind_value = reg.kind.value if hasattr(reg.kind, "value") else reg.kind
            # BH-178 — a label printer that was never told which protocol to
            # speak. Two ways to end up here: registered before PrintProtocol
            # existed (no field at all), or registered while the fallback was
            # a blanket ESC/POS, which left it silent on TSPL hardware. Both
            # are our guess, not the operator's, so both get corrected.
            #
            # Read the RAW entry, never the validated object: pydantic fills a
            # missing `protocol_source` with "default", so asking the model
            # cannot tell "we guessed" from "the file predates the field". In
            # that older era `kind == label` already defaulted to TSPL, so an
            # ESC/POS label printer from back then is a DELIBERATE choice — a
            # receipt printer running labels on continuous tape, say. Flipping
            # it would destroy that setup, and the rewrite below makes it
            # unrecoverable (found by review).
            had_protocol = "protocol" in entry
            ours = entry.get("protocol_source") == "default"
            if kind_value == "label" and reg.protocol != PrintProtocol.tspl and (
                not had_protocol or ours
            ):
                reg.protocol = PrintProtocol.tspl
                # Not "the ESC/POS default" — this is a migrated registration,
                # and support should be able to see that at a glance.
                reg.protocol_source = "legacy-upgrade"
                upgraded = True
            self._registrations[reg.descriptor.id] = reg
        if upgraded:
            try:
                self.save()
            except Exception as exc:  # noqa: BLE001 — a read-only path, a gone directory
                # Losing the migration is a printer that needs re-registering.
                # Failing here would take the whole manager down with it:
                # `load()` runs inside the app's lifespan, unguarded.
                logger.warning(
                    "could not write the migrated printers.json (%s) — "
                    "the correction holds for this run only", exc,
                )
        logger.info("loaded %d registered printers", len(self._registrations))

    def save(self) -> None:
        # Entries we could not parse ride along untouched. Writing only what we
        # understood would delete them, and every registration change calls
        # this — so the loss would arrive later, by an unrelated hand (BH-178,
        # found by the second review round).
        payload = {
            "printers": [r.model_dump() for r in self._registrations.values()]
            + self._unreadable,
        }
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
        # Which wire protocol this printer speaks, in falling order of
        # confidence, recording WHERE the answer came from so support never
        # has to guess.
        #
        # BH-160 took the protocol from the DEVICE rather than the role:
        # picking "receipt" on label hardware used to hand it ESC/POS, which
        # the firmware accepts and throws away while we report success.
        #
        # BH-178 — but dropping the role entirely was too much. A model this
        # table has never seen (every network printer is discovered as plain
        # "Network printer <ip>") fell through to ESC/POS, so a label printer
        # registered as one printed NOTHING and the manager still answered
        # "printed". The role is a weaker signal than the model, not a useless
        # one: someone registering a label printer is telling us what it is.
        if req.protocol is not None:
            protocol = req.protocol
            protocol_source = "operator"
        else:
            detected = protocol_for_model(descriptor.label)
            kind_value = req.kind.value if hasattr(req.kind, "value") else req.kind
            if detected is not None:
                protocol = PrintProtocol(detected)
                protocol_source = "model-table"
            elif kind_value == "label":
                protocol = PrintProtocol.tspl
                protocol_source = "label-kind"
            else:
                protocol = PrintProtocol.escpos
                protocol_source = "default"
        kwargs = dict(
            descriptor=descriptor,
            kind=req.kind,
            nickname=req.nickname,
            paper_width=req.paper_width,
            render_mode=req.render_mode,
            code_page=req.code_page,
            drawer_pin=req.drawer_pin,
            protocol=protocol,
            protocol_source=protocol_source,
        )
        if req.label_height is not None:
            kwargs["label_height"] = req.label_height
        if req.label_gap is not None:
            kwargs["label_gap"] = req.label_gap
        reg = PrinterRegistration(**kwargs)
        self._registrations[descriptor.id] = reg
        # BH-160 — закешований пристрій тримає СТАРИЙ конфіг: ширину паперу,
        # протокол, режим рендеру, пін каси. Доти `register()` його не чіпав
        # (на відміну від `unregister()`), тож перереєстрація з іншою роллю чи
        # мовою не діяла до перезапуску менеджера: оператор міняв налаштування,
        # тиснув тест — і отримував стару поведінку. Знайдено живим прогоном:
        # реєстрація на 58 мм лишала джоби 80-мілімитровими.
        self._drop_cached_device(descriptor.id)
        self.save()
        return reg

    def _drop_cached_device(self, printer_id: str) -> None:
        """Викинути закешований пристрій, щоб наступний друк зібрав його
        наново з актуальної реєстрації."""
        device = self._devices.pop(printer_id, None)
        if device is None:
            return
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Синхронний виклик (тест, CLI) — циклу немає, і роз'єднувати
            # нічого: пристрій уже викинуто з кешу, а з'єднання підбере
            # збирач сміття. Головне — не впасти тут.
            return
        # Посилання на задачу треба ТРИМАТИ: `create_task` не володіє нею, і
        # збирач сміття може знести її до завершення — зʼєднання лишиться
        # відкритим, а в лозі буде «Task was destroyed but it is pending».
        # Раніше це стосувалось лише видалення принтера, тепер — кожної
        # перереєстрації, тобто на порядок частіше.
        task = asyncio.create_task(device.disconnect())
        self._pending_disconnects.add(task)
        task.add_done_callback(self._pending_disconnects.discard)
        # BH-168, друге коло ревʼю: пристрій зникає з кешу ДО того, як
        # `disconnect()` хоча б почався, а запис, що вже йде в потоці пристрою,
        # доїде лише за десятки секунд. Для `/busy` (BH-164) такий пристрій
        # мусить лишатись видимим, доки не допише.
        self._draining.add(device)

    def devices_for_busy(self) -> list:
        """Пристрої, про чиї джоби треба питати перед тим, як убити процес:
        живі в кеші плюс викинуті, що ще дописують."""
        self._draining = {
            d for d in self._draining
            if callable(getattr(d, "pending_jobs", None)) and d.pending_jobs() > 0
        }
        return [*self._devices.values(), *self._draining]

    def add_manual_descriptor(
        self,
        *,
        host: str,
        port: int,
        label: Optional[str] = None,
    ) -> PrinterDescriptor:
        """Register-by-IP for a NETWORK printer that discovery can't find
        (e.g. an Epson RT fiscal printer on the EpsonFPMate HTTP port, or the
        fiscal_epos emulator). Builds a stable network descriptor and caches it
        so `register()` can bind it — mirrors TerminalRegistry.add_manual_descriptor."""
        descriptor = PrinterDescriptor(
            id=make_id(PrinterTransport.network, host, str(port)),
            transport=PrinterTransport.network,
            label=label or f"{host}:{port}",
            network=NetworkAddress(host=host, port=port),
        )
        self._last_discovery[descriptor.id] = descriptor
        return descriptor

    def add_manual_descriptor_usb(
        self,
        *,
        vendor_id: int,
        product_id: int,
        in_ep: int,
        out_ep: int,
        serial: Optional[str] = None,
        label: Optional[str] = None,
    ) -> PrinterDescriptor:
        """Register-by-VID/PID for a USB printer that discovery can't see —
        a vendor-specific-class unit the scan skips, or one CUPS is holding.
        Read the values off `scripts/usb_probe.py`. The id is computed exactly
        like discover_usb() would, so if a later scan does surface the printer
        it reuses this same registration instead of creating a duplicate.
        Mirrors add_manual_descriptor (which is network-only)."""
        descriptor = PrinterDescriptor(
            id=make_id(
                PrinterTransport.usb,
                f"{vendor_id:04x}",
                f"{product_id:04x}",
                serial or "",
            ),
            transport=PrinterTransport.usb,
            label=label or f"USB printer {vendor_id:04x}:{product_id:04x}",
            usb=UsbAddress(
                vendor_id=vendor_id,
                product_id=product_id,
                in_ep=in_ep,
                out_ep=out_ep,
                serial=serial,
            ),
        )
        self._last_discovery[descriptor.id] = descriptor
        return descriptor

    def unregister(self, printer_id: str) -> None:
        if printer_id not in self._registrations:
            raise UnknownPrinter(printer_id)
        self._registrations.pop(printer_id)
        self._drop_cached_device(printer_id)
        self.save()

    # ---------- device access ----------

    async def get_device(self, printer_id: str) -> PrinterDevice:
        device = self._devices.get(printer_id)
        if device is not None:
            # Reconnect a cached device whose handle dropped — e.g. a previous
            # print hit a socket/USB error and the worker cleared it — instead
            # of handing back a dead one, so the next print self-heals rather
            # than failing with "printer unavailable". connect() never raises.
            if not device.is_connected():
                await device.connect()
            return device
        reg = self.get_registration(printer_id)
        device = self._build_device(reg)
        if reg.descriptor.transport in (
            PrinterTransport.usb.value,
            PrinterTransport.network.value,
            PrinterTransport.windows_spooler.value,
        ):
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
            # BH-160 — пристрою треба знати, якою мовою з ним говорити. Доти
            # протокол читав ЛИШЕ `/print/label`, тож TSPL-принтер не міг
            # надрукувати ні чек, ні кухонний квиток: решта маршрутів жорстко
            # слали ESC/POS, а прошивка їх мовчки ковтала.
            "protocol": (
                reg.protocol.value if hasattr(reg.protocol, "value") else reg.protocol
            ),
            "label_gap": getattr(reg, "label_gap", 2.25),
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
        elif transport == "windows_spooler":
            win = descriptor.windows
            cfg.update({
                "connection": "windows_spooler",
                "printer_name": win.printer_name if win else "",
            })
        elif transport == "bluetooth":
            raise NotImplementedError("bluetooth transport is Phase 2")
        return PrinterDevice(descriptor.id, cfg)
