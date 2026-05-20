"""Discover printers across transports.

USB walks the bus and picks every device exposing a printer-class
interface (`bInterfaceClass == 7`). Network combines mDNS browsing
(printers that announce themselves) with a port-9100 scan of the host's
own /24 (anything not announcing but listening on the raw print port).
Bluetooth stays best-effort for now — the cross-platform Python BT
story is fiddly enough that the operator hand-registers paired devices.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import logging
import socket
from typing import Optional

import usb.core
import usb.util

from src.models.printer import (
    NetworkAddress,
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


RAW_PRINT_PORT = 9100  # ESC/POS / PCL raw socket port, universal across vendors
IPP_PORT = 631
MDNS_SERVICES = (
    "_pdl-datastream._tcp.local.",  # HP / generic raw-9100 printers
    "_ipp._tcp.local.",  # Apple AirPrint / IPP printers (Epson TM-i, Star)
    "_printer._tcp.local.",  # Generic LPR
    "_escpos._tcp.local.",  # Our own future broadcast — Phase 2
)


def _local_subnet() -> Optional[ipaddress.IPv4Network]:
    """Find the host's primary /24 — what we'll port-scan for printers.
    Uses a UDP-connect trick to read the default-route interface IP
    without resolving DNS or relying on netifaces."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't actually send anything; just makes the kernel pick the
        # outgoing interface.
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    try:
        # /24 covers the typical SOHO router subnet.
        return ipaddress.ip_network(f"{local_ip}/24", strict=False)
    except ValueError:
        return None


def _probe_tcp(host: str, port: int, timeout: float = 0.3) -> bool:
    """True iff `host:port` accepts a TCP connect within `timeout`."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _reverse_dns(host: str) -> Optional[str]:
    try:
        name, *_ = socket.gethostbyaddr(host)
        return name
    except (socket.herror, OSError):
        return None


def _network_descriptor(host: str, port: int, label: str) -> PrinterDescriptor:
    return PrinterDescriptor(
        id=make_id(PrinterTransport.network, host, str(port)),
        transport=PrinterTransport.network,
        label=label,
        manufacturer=None,
        product=None,
        network=NetworkAddress(host=host, port=port),
    )


def _discover_mdns(timeout: float = 2.0) -> list[PrinterDescriptor]:
    """Browse mDNS for printer services. Quiet timeout — printers that
    don't announce themselves fall through to the port-scan path."""
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except Exception:  # noqa: BLE001
        logger.warning("zeroconf unavailable, skipping mDNS discovery")
        return []

    found: dict[str, PrinterDescriptor] = {}

    class _Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=int(timeout * 1000))
            if not info:
                return
            for raw in info.parsed_addresses():
                host = raw
                # Raw-9100 stays raw-9100; everything else mDNS sends us is
                # an IPP/LPR endpoint — still printable via the same socket
                # for ESC/POS-capable units, but the user picks the port
                # via the registration form if needed.
                port = (
                    RAW_PRINT_PORT
                    if "_pdl-datastream" in type_ or "_escpos" in type_
                    else info.port or IPP_PORT
                )
                label = info.name.split("._", 1)[0] if "._" in info.name else info.name
                descriptor = _network_descriptor(host, port, label)
                found[f"{host}:{port}"] = descriptor

        def update_service(self, zc, type_, name):
            self.add_service(zc, type_, name)

        def remove_service(self, zc, type_, name):
            return

    zc = Zeroconf()
    try:
        listener = _Listener()
        for service in MDNS_SERVICES:
            ServiceBrowser(zc, service, listener)
        # Synchronous wait — zeroconf populates `found` from background
        # threads, we just sleep through the discovery window.
        import time

        time.sleep(timeout)
    finally:
        try:
            zc.close()
        except Exception:  # noqa: BLE001
            pass
    return list(found.values())


def _discover_lan_scan(timeout: float = 0.3) -> list[PrinterDescriptor]:
    """Probe every host on the local /24 for TCP 9100. Concurrent so the
    full sweep finishes in roughly `timeout` seconds, not 254×timeout."""
    subnet = _local_subnet()
    if subnet is None:
        return []
    hosts = [str(h) for h in subnet.hosts()]
    found: list[PrinterDescriptor] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        future_to_host = {
            pool.submit(_probe_tcp, host, RAW_PRINT_PORT, timeout): host
            for host in hosts
        }
        for future in concurrent.futures.as_completed(future_to_host):
            host = future_to_host[future]
            try:
                if not future.result():
                    continue
            except Exception:  # noqa: BLE001
                continue
            label = _reverse_dns(host) or f"Network printer {host}"
            found.append(_network_descriptor(host, RAW_PRINT_PORT, label))
    return found


def discover_network() -> list[PrinterDescriptor]:
    """Combined mDNS + port-9100 sweep. Dedupes on `host:port` so a
    printer that announces over mDNS and also answers raw-9100 only
    shows up once.

    Manager is local; we only scan the host's own /24 — never the
    internet, never an arbitrary CIDR. Operators on multi-VLAN setups
    can run the manager on each segment they care about.
    """
    found: list[PrinterDescriptor] = []
    found.extend(_discover_mdns())
    found.extend(_discover_lan_scan())
    # Dedupe — preserve insertion order so mDNS hits (richer labels) win.
    seen: set[str] = set()
    unique: list[PrinterDescriptor] = []
    for d in found:
        key = f"{d.network.host}:{d.network.port}" if d.network else d.id
        if key in seen:
            continue
        seen.add(key)
        unique.append(d)
    return unique


SSI_TCP_PORT = 3000  # SSI ECR JSON framed-TCP transport (doc §1.1)


def discover_network_terminals(
    timeout: float = 0.3,
    probe_timeout: float = 2.0,
) -> list:
    """LAN scan for SSI-protocol POS terminals.

    Two-phase to keep the round-trip count low: fast TCP connect on
    port 3000 across the host's /24 → only on hosts that accept the
    connect do we spend the heavier SSI `PingDevice` probe (which
    speaks the framed protocol and waits for a reply). The probe is
    what tells a real SSI terminal apart from "something listening on
    3000" (mDNS responder, a developer box running a service, etc).
    """
    from src.services.terminals.ssi import SSITerminalAdapter

    subnet = _local_subnet()
    if subnet is None:
        return []
    candidates: list[str] = []
    hosts = [str(h) for h in subnet.hosts()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        future_to_host = {
            pool.submit(_probe_tcp, host, SSI_TCP_PORT, timeout): host
            for host in hosts
        }
        for future in concurrent.futures.as_completed(future_to_host):
            host = future_to_host[future]
            try:
                if future.result():
                    candidates.append(host)
            except Exception:  # noqa: BLE001
                continue

    if not candidates:
        return []

    # Probe is async (asyncio.open_connection) so we run the small
    # batch sequentially on a fresh event loop — keeps the function
    # callable from both sync (FastAPI startup) and async (route via
    # asyncio.to_thread) contexts without nested-loop trouble.
    async def _probe_all() -> list:
        out: list = []
        for host in candidates:
            descriptor = await SSITerminalAdapter.probe(host, SSI_TCP_PORT)
            if descriptor is not None:
                out.append(descriptor)
        return out

    import asyncio

    return asyncio.run(_probe_all())


def discover_bluetooth() -> list[PrinterDescriptor]:
    """Best-effort Classic Bluetooth scrape.

    Classic Bluetooth from Python is platform-specific and historically
    flaky on macOS (no BlueZ, no pybluez). Until we ship a native iOS /
    Android wrapper that owns the BT stack, the most reliable path is
    asking the OS for *already paired* devices and offering them to the
    operator — they pair once in System Settings, then they're here.
    """
    found: list[PrinterDescriptor] = []
    import platform
    import shutil
    import subprocess

    system = platform.system()
    if system == "Linux" and shutil.which("bluetoothctl"):
        try:
            result = subprocess.run(
                ["bluetoothctl", "devices"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in result.stdout.splitlines():
                # `Device AA:BB:CC:DD:EE:FF Some Printer Name`
                parts = line.split(maxsplit=2)
                if len(parts) < 3 or parts[0] != "Device":
                    continue
                mac, name = parts[1], parts[2]
                # Filter on names that look printer-y so we don't list
                # the operator's headphones. False positives are
                # tolerable — registering a non-printer just fails to
                # connect.
                if not any(
                    keyword in name.lower()
                    for keyword in ("print", "pos", "rpp", "star", "epson", "escpos")
                ):
                    continue
                found.append(
                    PrinterDescriptor(
                        id=make_id(PrinterTransport.bluetooth, mac),
                        transport=PrinterTransport.bluetooth,
                        label=name,
                        manufacturer=None,
                        product=None,
                        bluetooth={"mac": mac, "channel": 1},  # type: ignore[arg-type]
                    )
                )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("bluetoothctl scrape failed: %s", exc)
    elif system == "Darwin":
        # macOS has no userland-visible classic-BT API that python-escpos
        # can drive — IOBluetooth is Objective-C only. Skip with a log.
        logger.debug("bluetooth discovery skipped on macOS (no BlueZ)")
    return found


def discover_all() -> list[PrinterDescriptor]:
    """Aggregate every transport into one list."""
    out: list[PrinterDescriptor] = []
    out.extend(discover_usb())
    out.extend(discover_network())
    out.extend(discover_bluetooth())
    return out
