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

USB_CLASS_PRINTER = 0x07          # standard USB Printer Class
USB_CLASS_VENDOR_SPECIFIC = 0xff  # where cheap ESC/POS clones hide
USB_CLASS_UNSPECIFIED = 0x00      # "see interface descriptors" — some too
EP_TRANSFER_BULK = 0x02

# USB vendor IDs of thermal-printer controllers that commonly enumerate as
# vendor-specific (class 0xff) or a USB-serial bridge instead of the standard
# Printer class (0x07) — so the strict class-0x07 filter would silently skip
# them. A bulk in+out interface on any of these is treated as a printer even
# when the class byte doesn't say so. Extend as new hardware is field-tested.
KNOWN_PRINTER_VENDORS = {
    0x0416,  # Winbond / Nuvoton — SP-POS58 family & generic 58mm POS clones
    0x0483,  # STMicroelectronics — SPRT / STM32-based thermals (SP-POS58IV)
    0x0fe6,  # ICS Advent / Zjiang — Rongta RP58 / RG-P58D & Zjiang clones
    0x28e9,  # GigaDevice (GD32) — generic thermal clones
    0x1a86,  # QinHeng CH340 — USB-serial bridge on serial-attached thermals
}


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


def _is_termux() -> bool:
    """True iff we're running under Termux on Android. Marker env var
    is set by Termux itself in every shell."""
    import os
    return os.environ.get("PREFIX", "").startswith("/data/data/com.termux/")


def _select_printer_interface(dev):
    """Pick the first printer-ish interface on `dev`.

    Returns `(in_ep, out_ep, match_kind)` or None. A standard USB
    Printer-class interface always wins over a heuristic match on the same
    device, so we scan every interface before settling on a fallback:

      1. ``printer-class``   — bInterfaceClass 0x07. Unambiguous, taken
         immediately.
      2. ``vendor-specific`` — class 0xff / 0x00 with bulk in+out endpoints.
         This is where cheap ESC/POS thermals (STMicro / Winbond / Rongta
         clones) that skip the Printer class enumerate. Gated on bulk
         endpoints so we don't list HID / storage / hub interfaces.
      3. ``known-vendor``    — any bulk in+out interface whose device VID is
         in ``KNOWN_PRINTER_VENDORS`` (covers CDC/serial-bridge units whose
         class byte isn't 0xff).

    Both a bulk-IN and a bulk-OUT endpoint are required because
    ``escpos.printer.Usb`` (and status reads) need both — unidirectional
    (bulk-OUT-only) printers still fall through to manual registration.
    """
    fallback = None
    for cfg in dev:
        for iface in cfg:
            in_ep, out_ep = _bulk_endpoints(iface)
            if in_ep is None or out_ep is None:
                continue
            cls = iface.bInterfaceClass
            if cls == USB_CLASS_PRINTER:
                return in_ep, out_ep, "printer-class"
            if fallback is not None:
                continue  # already have a fallback; keep looking for class 0x07
            if cls in (USB_CLASS_VENDOR_SPECIFIC, USB_CLASS_UNSPECIFIED):
                fallback = (in_ep, out_ep, "vendor-specific")
            elif dev.idVendor in KNOWN_PRINTER_VENDORS:
                fallback = (in_ep, out_ep, "known-vendor")
    return fallback


def discover_usb() -> list[PrinterDescriptor]:
    # On Termux/Android pyusb's libusb backend can't reach the system
    # USB stack without per-device termux-usb permissions, and even
    # then the workflow is one-at-a-time + user-prompted. We've
    # decided to support only network printers on Android — skip
    # cleanly so the operator doesn't see noisy NoBackendError logs.
    if _is_termux():
        logger.debug("USB discovery skipped on Termux/Android (use network printers)")
        return []
    found: list[PrinterDescriptor] = []
    for dev in usb.core.find(find_all=True):
        try:
            selection = _select_printer_interface(dev)
        except Exception as exc:  # noqa: BLE001 — libusb can throw per-iface
            logger.debug(
                "USB scan: skipping %04x:%04x (descriptor read failed: %s)",
                getattr(dev, "idVendor", 0), getattr(dev, "idProduct", 0), exc,
            )
            continue
        if selection is None:
            continue
        in_ep, out_ep, match_kind = selection
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
        if match_kind != "printer-class":
            logger.info(
                "USB scan: %04x:%04x matched via %s (non-standard class) — %s",
                dev.idVendor, dev.idProduct, match_kind, descriptor.label,
            )
        found.append(descriptor)
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
    """Find the host's primary /24 via UDP-connect to a public IP.

    Kept for backwards-compatibility — internally we now use
    `_local_subnets()` which enumerates ALL local interfaces, but a few
    older call sites (printer LAN scan + tests) still expect the
    "outgoing default route" single-subnet behavior.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    try:
        return ipaddress.ip_network(f"{local_ip}/24", strict=False)
    except ValueError:
        return None


def _local_subnets() -> list[ipaddress.IPv4Network]:
    """All local /24 subnets the host has an interface on.

    Why not just `_local_subnet()`: the UDP-connect trick returns the
    *outgoing* default-route IP only. On a tablet with both cellular
    (default outgoing) and Wi-Fi/hotspot (separate subnet for local
    devices), it misses the subnet where the POS terminal actually
    lives.

    Three independent sources, deduplicated:
      1. UDP-connect default-route IP (handles most desktops/Pi setups
         and confirms a working interface)
      2. `socket.if_nameindex()` + per-interface `ioctl(SIOCGIFADDR)`
         via fcntl — covers Linux/Android/macOS
      3. `getaddrinfo(gethostname())` — fallback for stdlib-only hosts

    Each detected IP becomes a /24. Loopback and link-local (169.254.x)
    are dropped. Result is ordered: default-route first, others after,
    so terminals on the "primary" network are still found fastest.
    """
    out: list[ipaddress.IPv4Network] = []
    seen: set[ipaddress.IPv4Network] = set()

    def _add(ip: str) -> None:
        try:
            addr = ipaddress.IPv4Address(ip)
        except (ipaddress.AddressValueError, ValueError):
            return
        if addr.is_loopback or addr.is_link_local or addr.is_multicast:
            return
        try:
            net = ipaddress.ip_network(f"{ip}/24", strict=False)
        except ValueError:
            return
        if net not in seen:
            seen.add(net)
            out.append(net)

    # Source 1 — UDP-connect (default outgoing route, e.g. cellular on
    # a phone or Wi-Fi on a desktop). First so it stays the primary.
    default = _local_subnet()
    if default is not None:
        seen.add(default)
        out.append(default)

    # Source 2 — per-interface ioctl. Linux/Android/macOS friendly,
    # avoids needing `iproute2` / netifaces. On Android, `if_nameindex`
    # OR the ioctl can raise PermissionError under the sandbox — catch
    # broadly so we just skip this source and fall through to source 3.
    try:
        import fcntl
        import struct
        SIOCGIFADDR = 0x8915
        try:
            iface_list = list(socket.if_nameindex())
        except (OSError, PermissionError):
            iface_list = []
        for _, name in iface_list:
            try:
                ifname_bytes = name.encode("utf-8")[:15]
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    packed = fcntl.ioctl(
                        s.fileno(),
                        SIOCGIFADDR,
                        struct.pack("256s", ifname_bytes),
                    )
                    ip = socket.inet_ntoa(packed[20:24])
                    _add(ip)
                finally:
                    s.close()
            except (OSError, ValueError, PermissionError):
                continue
    except (ImportError, AttributeError):
        # fcntl not available on Windows / some embedded Pythons.
        pass

    # Source 3 — /proc/net/route fallback. Linux/Android friendly, no
    # ioctl permissions needed; Termux reads it as the unprivileged
    # user. Format: each line is a routing entry with hex destination
    # + genmask. We collect every non-default-route destination, treat
    # its /24 as a candidate subnet.
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as fh:
            for raw in fh.readlines()[1:]:  # skip header
                parts = raw.strip().split()
                if len(parts) < 3:
                    continue
                dest_hex = parts[1]
                if dest_hex == "00000000":
                    continue  # default route — no useful subnet
                try:
                    # little-endian hex per kernel convention
                    dest = ".".join(
                        str(int(dest_hex[i:i + 2], 16))
                        for i in (6, 4, 2, 0)
                    )
                    _add(dest)
                except (ValueError, IndexError):
                    continue
    except (OSError, IOError):
        pass

    # Source 4 — fallback via gethostname. On Android emulator this
    # usually only resolves to loopback (which we'll filter), but on
    # desktops it picks up the LAN IP when ioctl + /proc both missed.
    try:
        for info in socket.getaddrinfo(
            socket.gethostname(), None, family=socket.AF_INET,
        ):
            _add(info[4][0])
    except (socket.gaierror, OSError):
        pass

    return out


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


SSI_TCP_PORT = 3000     # SSI ECR JSON framed-TCP transport (doc §1.1)
PB_TCP_PORT = 2000      # PrivatBank ECR JSON direct-terminal port (spec §1)
POSAPI_TCP_PORT = 8080  # Printec PosAPI bridge (Raiffeisen / PUMB)
BPOS_TCP_PORT = 8888    # BPOS1 / BPOS Light bridge (Pivdenny / Sense)
OSCHAD_TCP_PORT = 7777  # Oschadbank ECR bridge


def _terminal_port_adapters() -> dict[int, list]:
    """Map scan port → adapter classes whose `probe()` to try on it.

    The bank-JSON ports (2000/3000) are cross-probed with SSI *and* PB
    because either protocol can end up on either port depending on the
    unit; each bridge port maps to its one protocol. The handshake inside
    `probe()` is what confirms a real terminal vs. "something listening".
    Imported lazily to avoid a start-up import cycle (adapters import
    models which import nothing from scan, but keep the original lazy
    style for parity).
    """
    from src.services.terminals.bpos import BposTerminalAdapter
    from src.services.terminals.oschad import OschadTerminalAdapter
    from src.services.terminals.posapi import PosApiTerminalAdapter
    from src.services.terminals.privatbank import PrivatBankTerminalAdapter
    from src.services.terminals.ssi import SSITerminalAdapter

    return {
        SSI_TCP_PORT: [SSITerminalAdapter, PrivatBankTerminalAdapter],
        PB_TCP_PORT: [PrivatBankTerminalAdapter, SSITerminalAdapter],
        POSAPI_TCP_PORT: [PosApiTerminalAdapter],
        BPOS_TCP_PORT: [BposTerminalAdapter],
        OSCHAD_TCP_PORT: [OschadTerminalAdapter],
    }


def discover_network_terminals(
    timeout: float = 0.3,
    probe_timeout: float = 2.0,
) -> list:
    """LAN scan for every supported POS-terminal protocol.

    Two-phase: fast TCP connect across the host's /24 on every known
    terminal/bridge port, then each open host:port is probed with the
    adapters mapped to that port. The bank-specific handshake is what
    tells a real terminal apart from "something listening on that port".

    Covered: SSI ECR JSON (Mono, 3000), PrivatBank JSON (2000),
    Printec PosAPI (Raif/PUMB bridge, 8080), BPOS1/Light (Pivdenny/Sense
    bridge, 8888), Oschad ECR (bridge, 7777).
    """
    port_adapters = _terminal_port_adapters()
    scan_ports = list(port_adapters.keys())

    subnets = _local_subnets()
    if not subnets:
        logger.warning(
            "terminal discovery: could not detect any local subnet — no "
            "Wi-Fi/network interface? (all enumeration methods failed)",
        )
        return []
    hosts: list[str] = []
    for sn in subnets:
        hosts.extend(str(h) for h in sn.hosts())
    logger.info(
        "terminal discovery: scanning %d subnet(s) %s (%d hosts total) "
        "ports=%s tcp_timeout=%.1fs",
        len(subnets), [str(s) for s in subnets], len(hosts),
        scan_ports, timeout,
    )
    # Collect open host:port pairs across every terminal port.
    open_pairs: list[tuple[str, int]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        future_map: dict = {}
        for host in hosts:
            for port in scan_ports:
                future_map[pool.submit(_probe_tcp, host, port, timeout)] = (host, port)
        for future in concurrent.futures.as_completed(future_map):
            host, port = future_map[future]
            try:
                if not future.result():
                    continue
            except Exception:  # noqa: BLE001
                continue
            open_pairs.append((host, port))

    open_by_port = {
        port: [h for h, p in open_pairs if p == port] for port in scan_ports
    }
    logger.info(
        "terminal discovery: TCP-open hosts %s",
        {p: (v or "[]") for p, v in open_by_port.items()},
    )
    from src.services.log_uplink import emit_event
    emit_event(
        "discovery_run",
        subnets=[str(s) for s in subnets],
        hosts_scanned=len(hosts),
        ssi_port_found=len(open_by_port.get(SSI_TCP_PORT, [])),
        pb_port_found=len(open_by_port.get(PB_TCP_PORT, [])),
        posapi_port_found=len(open_by_port.get(POSAPI_TCP_PORT, [])),
        bpos_port_found=len(open_by_port.get(BPOS_TCP_PORT, [])),
        oschad_port_found=len(open_by_port.get(OSCHAD_TCP_PORT, [])),
    )
    if not open_pairs:
        logger.warning(
            "terminal discovery: nothing answered on ports %s across %s. "
            "Common causes: terminal on another subnet our interface scan "
            "didn't enumerate (CGNAT/guest isolation); the bank bridge "
            "(Printec/BPOS/Oschad) not running next to the manager; host "
            "firewall; terminal offline. If you know the IP, use "
            "'Додати термінал вручну' in the dashboard.",
            scan_ports, [str(s) for s in subnets],
        )
        return []

    # Probe every open host:port with the adapters mapped to that port.
    # Dedup by (host, port, adapter) so we don't double-report.
    async def _probe_all() -> list:
        out: list = []
        seen: set[str] = set()
        for host, port in open_pairs:
            for adapter in port_adapters.get(port, []):
                key = f"{host}:{port}:{adapter.__name__}"
                if key in seen:
                    continue
                seen.add(key)
                try:
                    descriptor = await adapter.probe(host, port)
                except Exception:  # noqa: BLE001
                    descriptor = None
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
    # Termux/Android: BluetoothAdapter is a Java framework API
    # accessible only from an Activity/Service with the BLUETOOTH
    # permission — Python in Termux can't reach it without a companion
    # APK. Skip until we ship one.
    if _is_termux():
        logger.debug("Bluetooth discovery skipped on Termux/Android (needs companion APK)")
        return []
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
    """Aggregate every transport into one list.

    Each transport is wrapped — a failure in one shouldn't sink the
    whole scan. On Termux/Android pyusb can't reach the system USB
    stack (NoBackendError); on a machine without a network stack
    zeroconf throws; bluetoothctl may be missing. None of those are
    operator-actionable from the dashboard, and 500 on Discover is a
    worse UX than "nothing found yet — plug your printer in."
    """
    out: list[PrinterDescriptor] = []
    for transport_name, fn in (
        ("USB", discover_usb),
        ("network", discover_network),
        ("Bluetooth", discover_bluetooth),
    ):
        try:
            out.extend(fn())
        except Exception as exc:  # noqa: BLE001 — keep going on any failure
            logger.warning(
                "%s discovery failed: %s (continuing with other transports)",
                transport_name,
                exc,
            )
    return out
