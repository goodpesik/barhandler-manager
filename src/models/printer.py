"""Persistent printer registry models.

A `PrinterDescriptor` is what the manager hands the frontend during
discovery — enough info to identify a physical printer and reconnect to
it later. The `id` is stable across reboots and replugs: it's a hash of
`transport + vendor:product:serial` for USB, `transport + host:port` for
network, `transport + mac` for Bluetooth, so the frontend can store one
ID and rely on the manager to find the printer again.

A `PrinterRegistration` adds the operator's choice — role (receipt vs
kitchen vs label) and a custom label — and is persisted to `printers.json`.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class PrinterKind(str, Enum):
    receipt = "receipt"
    kitchen = "kitchen"
    label = "label"


class PrinterTransport(str, Enum):
    usb = "usb"
    network = "network"
    bluetooth = "bluetooth"


class PrintProtocol(str, Enum):
    """Wire protocol the printer's firmware speaks.

    Most 58/80mm thermal receipt printers default to ESC/POS — that's
    what `esc._raw(GS v 0 ...)` expects. Dedicated label printers like
    Xprinter XP-246B/235B ship in "LABEL" / TSPL mode (`Print mode:
    LABEL` on self-test) and silently ignore ESC/POS commands. For
    those we send TSPL bitmap commands instead.

    Operators can flip an XP-246B to ESC/POS via the Xprinter Diag
    Tool, but TSPL is the better default for label printers because
    it has proper label/gap awareness and tear-off positioning.
    """

    escpos = "escpos"
    tspl = "tspl"


class UsbAddress(BaseModel):
    vendor_id: int
    product_id: int
    in_ep: int
    out_ep: int
    serial: Optional[str] = None


class NetworkAddress(BaseModel):
    host: str
    port: int = 9100


class BluetoothAddress(BaseModel):
    mac: str
    channel: int = 1


class PrinterDescriptor(BaseModel):
    """Everything the manager needs to reach a physical printer."""

    model_config = ConfigDict(use_enum_values=True)

    id: str                       # stable across reboots — see make_id()
    transport: PrinterTransport
    label: str                    # human-readable, e.g. "STMicro POS"
    manufacturer: Optional[str] = None
    product: Optional[str] = None
    usb: Optional[UsbAddress] = None
    network: Optional[NetworkAddress] = None
    bluetooth: Optional[BluetoothAddress] = None


class PrinterRegistration(BaseModel):
    """User's persistent choice — which physical printer plays which role."""

    descriptor: PrinterDescriptor
    kind: PrinterKind = PrinterKind.receipt
    nickname: Optional[str] = None         # e.g. "Бар-чек" / "Кухня"
    paper_width: int = 58                  # mm: 58 (32 chars) or 80 (48 chars)
    render_mode: str = "bitmap"            # "bitmap" (default) | "native"
    code_page: Optional[str] = None        # only used when render_mode=native
    drawer_pin: Optional[int] = 0          # 0 / 1 / None to disable
    # Wire protocol — see PrintProtocol. Default escpos covers every
    # receipt-style printer; flip to tspl for dedicated label printers
    # (XP-246B / 235B / 237B) shipping in `Print mode: LABEL`.
    protocol: PrintProtocol = PrintProtocol.escpos
    # Label-specific layout (used by `/print/label` when protocol=tspl).
    # Default 48×25mm + 2.25mm gap matches the XP-246B factory roll.
    label_height: int = 25                 # mm — physical label length
    label_gap: float = 2.25                # mm — gap between labels

    @property
    def chars_per_line(self) -> int:
        return 48 if self.paper_width >= 80 else 32


class RegistrationRequest(BaseModel):
    """Body for POST /devices/register — frontend sends the discovered
    descriptor back along with its chosen role / nickname / paper config."""

    id: str = Field(min_length=1)
    kind: PrinterKind = PrinterKind.receipt
    nickname: Optional[str] = None
    paper_width: int = 58
    render_mode: str = "bitmap"
    code_page: Optional[str] = None
    drawer_pin: Optional[int] = 0
    protocol: Optional[PrintProtocol] = None  # None → auto: tspl for label, escpos otherwise
    label_height: Optional[int] = None
    label_gap: Optional[float] = None


def make_id(transport: PrinterTransport, *parts: str) -> str:
    """Generate a stable id for a physical printer.

    The hash inputs are transport + the immutable address bits — never
    the human label — so the same physical device always lands on the
    same id even if its manufacturer string changes between firmware
    revisions.
    """
    payload = ":".join([transport.value, *parts]).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:12]
