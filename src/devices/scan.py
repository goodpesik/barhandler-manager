"""Discover printers across transports.

USB works today — we walk the bus and pick every device exposing a
printer-class interface (`bInterfaceClass == 7`). The other transports
are scaffolded with no-op stubs so the frontend can call a single
discovery endpoint now and we wire the implementation up later without
changing the contract.

  Phase 2:
  - Network: mDNS/Bonjour for `_pdl-datastream._tcp` and `_ipp._tcp`
    (also raw nmap-style scan of 9100 if the user provides a CIDR).
  - Bluetooth: list paired devices via `bluetoothctl` or pybluez; ESC/POS
    over RFCOMM (channel 1 by default).
"""

from __future__ import annotations

import logging
from typing import Optional

import usb.core
import usb.util

from src.models.printer import (
    PrinterDescriptor,
    PrinterTransport,
    UsbAddress,
    make_id,
)

logger = logging.getLogger(__name__)

USB_CLASS_PRINTER = 0x07
EP_TRANSFER_BULK = 0x02


def _safe_string(dev, idx) -> Optional[str]:
    if not idx:
        return None
    try:
        return usb.util.get_string(dev, idx).strip() or None
    except Exception:
        return None


def _bulk_endpoints(iface) -> tuple[Optional[int], Optional[int]]:
    in_ep = out_ep = None
    for ep in iface:
        if (ep.bmAttributes & 0x03) != EP_TRANSFER_BULK:
            continue
        if (ep.bEndpointAddress & 0x80) and in_ep is None:
            in_ep = ep.bEndpointAddress
        elif not (ep.bEndpointAddress & 0x80) and out_ep is None:
            out_ep = ep.bEndpointAddress
    return in_ep, out_ep


def discover_usb() -> list[PrinterDescriptor]:
    found: list[PrinterDescriptor] = []
    for dev in usb.core.find(find_all=True):
        for cfg in dev:
            for iface in cfg:
                if iface.bInterfaceClass != USB_CLASS_PRINTER:
                    continue
                in_ep, out_ep = _bulk_endpoints(iface)
                if in_ep is None or out_ep is None:
                    continue
                manufacturer = _safe_string(dev, dev.iManufacturer)
                product = _safe_string(dev, dev.iProduct)
                serial = _safe_string(dev, dev.iSerialNumber)
                label_parts = [p for p in (manufacturer, product) if p] or [
                    f"USB printer {dev.idVendor:04x}:{dev.idProduct:04x}"
                ]
                descriptor = PrinterDescriptor(
                    id=make_id(
                        PrinterTransport.usb,
                        f"{dev.idVendor:04x}",
                        f"{dev.idProduct:04x}",
                        serial or "",
                    ),
                    transport=PrinterTransport.usb,
                    label=" ".join(label_parts),
                    manufacturer=manufacturer,
                    product=product,
                    usb=UsbAddress(
                        vendor_id=dev.idVendor,
                        product_id=dev.idProduct,
                        in_ep=in_ep,
                        out_ep=out_ep,
                        serial=serial,
                    ),
                )
                found.append(descriptor)
                break  # one printer-class interface per device is enough
    return found


def discover_network() -> list[PrinterDescriptor]:
    """Phase 2 — mDNS / nmap on port 9100. Returns empty for now."""
    logger.debug("network discovery not yet implemented (Phase 2)")
    return []


def discover_bluetooth() -> list[PrinterDescriptor]:
    """Phase 2 — pybluez or bluetoothctl. Returns empty for now."""
    logger.debug("bluetooth discovery not yet implemented (Phase 2)")
    return []


def discover_all() -> list[PrinterDescriptor]:
    """Aggregate every transport into one list."""
    out: list[PrinterDescriptor] = []
    out.extend(discover_usb())
    out.extend(discover_network())
    out.extend(discover_bluetooth())
    return out
