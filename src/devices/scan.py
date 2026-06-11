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


def _is_termux() -> bool:
    """True iff we're running under Termux on Android. Marker env var
    is set by Termux itself in every shell."""
    import os
    return os.environ.get("PREFIX", "").startswith("/data/data/com.termux/")


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


SSI_TCP_PORT = 3000  # SSI ECR JSON framed-TCP transport (doc §1.1)
PB_TCP_PORT = 2000   # PrivatBank ECR JSON direct-terminal port (spec §1)


def discover_network_terminals(
    timeout: float = 0.3,
    probe_timeout: float = 2.0,
) -> list:
    """LAN scan for SSI- and PrivatBank-protocol POS terminals.

    Two-phase: fast TCP connect across the host's /24 on both ports,
    then every open host:port is probed with BOTH SSI and PB protocols —
    because Mono terminals can listen on 2000 and PB terminals on 3000.
    The bank-specific handshake is what tells a real terminal apart from
    "something listening on that port".
    """
    from src.services.terminals.privatbank import PrivatBankTerminalAdapter
    from src.services.terminals.ssi import SSITerminalAdapter

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
        "ports SSI=%d PB=%d tcp_timeout=%.1fs",
        len(subnets), [str(s) for s in subnets], len(hosts),
        SSI_TCP_PORT, PB_TCP_PORT, timeout,
    )
    # Collect open host:port pairs — both protocols tried on each
    open_pairs: list[tuple[str, int]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        future_map: dict = {}
        for host in hosts:
            future_map[pool.submit(_probe_tcp, host, SSI_TCP_PORT, timeout)] = (host, SSI_TCP_PORT)
            future_map[pool.submit(_probe_tcp, host, PB_TCP_PORT, timeout)] = (host, PB_TCP_PORT)
        for future in concurrent.futures.as_completed(future_map):
            host, port = future_map[future]
            try:
                if not future.result():
                    continue
            except Exception:  # noqa: BLE001
                continue
            open_pairs.append((host, port))

    open_on_ssi_port = [h for h, p in open_pairs if p == SSI_TCP_PORT]
    open_on_pb_port  = [h for h, p in open_pairs if p == PB_TCP_PORT]
    logger.info(
        "terminal discovery: TCP-open hosts port%d=%s port%d=%s",
        SSI_TCP_PORT, open_on_ssi_port or "[]",
        PB_TCP_PORT,  open_on_pb_port  or "[]",
    )
    from src.services.log_uplink import emit_event
    emit_event(
        "discovery_run",
        subnets=[str(s) for s in subnets],
        hosts_scanned=len(hosts),
        ssi_port_found=len(open_on_ssi_port),
        pb_port_found=len(open_on_pb_port),
    )
    if not open_pairs:
        logger.warning(
            "terminal discovery: nothing answered on port %d (SSI) or %d (PB) "
            "across %s. Common causes: terminal on yet another subnet not "
            "enumerated by our interface scan (rare — usually CGNAT carrier "
            "isolating the terminal SIM from this device); guest-network "
            "client isolation; manager host firewall; terminal offline. "
            "If you know the terminal's IP, use 'Додати термінал вручну' "
            "in the dashboard.",
            SSI_TCP_PORT, PB_TCP_PORT, [str(s) for s in subnets],
        )
        return []

    # Probe every open host:port with BOTH protocols — a Mono terminal
    # may answer on port 2000, a PB terminal on port 3000.
    # Dedup by (host, port, protocol) so we don't double-report.
    async def _probe_all() -> list:
        out: list = []
        seen: set[str] = set()
        for host, port in open_pairs:
            for adapter, label in (
                (SSITerminalAdapter, "ssi"),
                (PrivatBankTerminalAdapter, "pb"),
            ):
                key = f"{host}:{port}:{label}"
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
