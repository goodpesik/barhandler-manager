"""Standalone USB diagnostic — what does libusb actually see on this box?

When discover_usb() returns nothing but the operator clearly has a
thermal printer plugged in, the question is one of:

  - libusb can't reach the system USB stack at all (0 devices)
  - libusb sees the device but it exposes no bulk in+out endpoints on
    any printer-ish interface (unidirectional / odd firmware) — use
    manual registration (POST /devices/register-usb-manual)
  - macOS CUPS driver is holding the device (you'll see it in
    /usr/bin/lpinfo but libusb returns "busy" / nothing)

discover_usb() now accepts, besides the standard Printer class (0x07),
vendor-specific (0xff) / unspecified (0x00) interfaces with bulk in+out
endpoints, and any bulk in+out interface on a known thermal-printer
vendor (see KNOWN_PRINTER_VENDORS in src/devices/scan.py) — that's where
cheap SP-POS58 / Rongta / STMicro clones hide.

Run via:
    ~/.barhandler-manager/.venv/bin/python scripts/usb_probe.py

Or pipe straight from GitHub:
    curl -fsSL https://raw.githubusercontent.com/goodpesik/barhandler-manager/main/scripts/usb_probe.py \\
        | ~/.barhandler-manager/.venv/bin/python
"""

import sys

try:
    import usb.core
    import usb.util
except ImportError as exc:
    print(f"pyusb not importable: {exc}")
    print("Make sure you're running this with the manager's venv interpreter.")
    sys.exit(1)


# Kept in sync with KNOWN_PRINTER_VENDORS in src/devices/scan.py. Duplicated
# (not imported) so this script stays runnable standalone via `curl | python`.
KNOWN_PRINTER_VENDORS = {0x0416, 0x0483, 0x0FE6, 0x28E9, 0x1A86}
BULK = 0x02


def safe_string(dev, index):
    if not index:
        return None
    try:
        return usb.util.get_string(dev, index)
    except Exception as exc:
        return f"<{type(exc).__name__}>"


def _has_bulk_in_out(iface) -> bool:
    has_in = has_out = False
    for ep in iface:
        if (ep.bmAttributes & 0x03) != BULK:
            continue
        if ep.bEndpointAddress & 0x80:
            has_in = True
        else:
            has_out = True
    return has_in and has_out


def _would_discover(dev) -> bool:
    """Mirror discover_usb()'s acceptance rule for this device."""
    try:
        for cfg in dev:
            for iface in cfg:
                cls = iface.bInterfaceClass
                if cls == 0x07 and _has_bulk_in_out(iface):
                    return True
                if not _has_bulk_in_out(iface):
                    continue
                if cls in (0x00, 0xFF) or dev.idVendor in KNOWN_PRINTER_VENDORS:
                    return True
    except Exception:
        return False
    return False


def main() -> None:
    try:
        devs = list(usb.core.find(find_all=True))
    except Exception as exc:
        print(f"usb.core.find() failed: {type(exc).__name__}: {exc}")
        print("This usually means libusb isn't installed or accessible.")
        print("On macOS: brew install libusb")
        sys.exit(1)

    print(f"Total USB devices visible: {len(devs)}")
    if not devs:
        print()
        print("Nothing — possible causes:")
        print(" 1) libusb missing.  brew install libusb")
        print(" 2) macOS hides USB without entitlements; try running")
        print("    the probe via `sudo`.")
        print(" 3) Some USB hubs need a re-plug after the driver loads.")
        return

    for d in devs:
        mfr = safe_string(d, d.iManufacturer) or "?"
        prod = safe_string(d, d.iProduct) or "?"
        serial = safe_string(d, d.iSerialNumber) or ""
        classes = set()
        endpoints = 0
        try:
            for cfg in d:
                for iface in cfg:
                    classes.add(f"0x{iface.bInterfaceClass:02x}")
                    endpoints += iface.bNumEndpoints
        except Exception as exc:
            classes = {f"<{type(exc).__name__}>"}
        printer_class = "0x07" in classes
        discoverable = _would_discover(d)
        if printer_class:
            marker = "  [PRINTER 0x07]"
        elif discoverable:
            marker = "  [PRINTER via relaxed filter]"
        else:
            marker = ""
        sn_part = f" sn={serial}" if serial else ""
        print(
            f"  {d.idVendor:04x}:{d.idProduct:04x}  classes={sorted(classes)}  "
            f"eps={endpoints}  {mfr} / {prod}{sn_part}{marker}"
        )

    print()
    discoverable = [d for d in devs if _would_discover(d)]
    print(f"Devices discover_usb() would now pick: {len(discoverable)}")
    if not discoverable:
        print(
            "→ Nothing matched even the relaxed filter (class 0x07, OR class\n"
            "  0x00/0xff with bulk in+out, OR a known printer VID). Likely:\n"
            "  • the printer exposes no bulk in+out pair (unidirectional /\n"
            "    odd firmware) — register it by hand with the VID:PID and\n"
            "    endpoint addresses above via POST /devices/register-usb-manual; OR\n"
            "  • macOS CUPS owns the device — remove it from System Settings\n"
            "    → Printers & Scanners so libusb can claim it."
        )


if __name__ == "__main__":
    main()
