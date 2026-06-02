"""Standalone USB diagnostic — what does libusb actually see on this box?

When discover_usb() returns nothing but the operator clearly has a
thermal printer plugged in, the question is one of:

  - libusb can't reach the system USB stack at all (0 devices)
  - libusb sees devices but our filter (bInterfaceClass == 0x07,
    standard USB Printer Class) skips vendor-specific ones — many
    Xprinter / Epson / generic thermals report 0xff
  - macOS CUPS driver is holding the device (you'll see it in
    /usr/bin/lpinfo but libusb returns "busy" / nothing)

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


def safe_string(dev, index):
    if not index:
        return None
    try:
        return usb.util.get_string(dev, index)
    except Exception as exc:
        return f"<{type(exc).__name__}>"


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
        marker = "  [PRINTER]" if printer_class else ""
        sn_part = f" sn={serial}" if serial else ""
        print(
            f"  {d.idVendor:04x}:{d.idProduct:04x}  classes={sorted(classes)}  "
            f"eps={endpoints}  {mfr} / {prod}{sn_part}{marker}"
        )

    print()
    printers = [d for d in devs if any(
        iface.bInterfaceClass == 0x07
        for cfg in d for iface in cfg
    )]
    print(f"Devices reporting USB Printer Class (0x07): {len(printers)}")
    if not printers:
        print(
            "→ discover_usb() filters on bInterfaceClass == 0x07. None of\n"
            "  your devices report it. Either:\n"
            "  • the printer uses vendor-specific class (0xff) — we'd need\n"
            "    a VID/PID whitelist or to relax the filter; OR\n"
            "  • macOS CUPS owns the device — try removing it from\n"
            "    System Settings → Printers and Scanners so libusb can\n"
            "    claim it."
        )


if __name__ == "__main__":
    main()
