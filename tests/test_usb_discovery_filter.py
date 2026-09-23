"""Unit tests for the relaxed USB printer-discovery filter.

`discover_usb()` used to accept ONLY interfaces with bInterfaceClass 0x07
(USB Printer Class). Cheap 58 mm thermals (SPRT SP-POS58IV, Rongta RG-P58D
and other STMicro/Winbond/Zjiang clones) frequently enumerate as
vendor-specific (0xff) or on a known printer VID with a non-standard class,
so the strict filter silently skipped them. These tests pin the new
behaviour: class 0x07 still matches, plus 0xff / known-vendor interfaces
that expose bulk in+out endpoints, while non-printer devices stay excluded.
"""

from __future__ import annotations

from unittest.mock import patch

from src.devices import scan


from tests._usb_fakes import (  # noqa: F401 — re-exported for other tests
    _FakeConfig,
    _FakeDevice,
    _FakeEndpoint,
    _FakeInterface,
    _bulk_pair,
    _device,
    _iface,
)


def _discover(devices):
    with patch.object(scan.usb.core, "find", return_value=devices), \
         patch.object(scan, "_is_termux", return_value=False):
        return scan.discover_usb()


def test_standard_printer_class_still_matches():
    dev = _device(0x0519, 0x0001, _iface(0x07))
    found = _discover([dev])
    assert len(found) == 1
    assert found[0].usb.vendor_id == 0x0519
    assert (found[0].usb.in_ep, found[0].usb.out_ep) == (0x81, 0x03)


def test_vendor_specific_class_with_bulk_endpoints_matches():
    # SP-POS58IV-style unit reporting class 0xff instead of 0x07.
    dev = _device(0x1234, 0x5678, _iface(0xFF))
    found = _discover([dev])
    assert len(found) == 1
    assert found[0].usb.product_id == 0x5678


def test_known_vendor_nonstandard_class_matches():
    # Rongta/Zjiang VID (0x0fe6) presenting a CDC-data-ish class byte.
    dev = _device(0x0FE6, 0x811E, _iface(0x0A))
    found = _discover([dev])
    assert len(found) == 1
    assert found[0].usb.vendor_id == 0x0FE6


def test_non_printer_device_is_ignored():
    # Unknown vendor, HID class (0x03) — must not be listed.
    dev = _device(0x046D, 0xC534, _iface(0x03))
    assert _discover([dev]) == []


def test_vendor_specific_without_bulk_endpoints_is_ignored():
    dev = _device(0x1234, 0x5678, _iface(0xFF, with_bulk=False))
    assert _discover([dev]) == []


def test_printer_class_wins_over_vendor_specific_on_same_device():
    # Composite device: a vendor-specific iface AND the real printer iface.
    dev = _device(0x0483, 0x5011, _iface(0xFF), _iface(0x07))
    found = _discover([dev])
    assert len(found) == 1  # one descriptor per device, printer-class chosen


def test_broken_descriptor_does_not_sink_the_whole_scan():
    class _Exploding(_FakeDevice):
        def __iter__(self):
            raise ValueError("libusb read error")

    good = _device(0x0FE6, 0x811E, _iface(0xFF))
    bad = _Exploding(0x9999, 0x9999, [])
    found = _discover([bad, good])
    assert [d.usb.vendor_id for d in found] == [0x0FE6]


# ---------- what the scan walked past (BH-179) ----------
#
# A printer that is plugged in and still missing from the result used to look
# exactly like one that is not plugged in at all: the skip was logged at DEBUG
# and nothing reached the caller. Support had to talk an operator through
# running scripts/usb_probe.py by hand.


def _report(devices) -> dict:
    """Run a scan and return the report it filled in for THIS call."""
    report: dict = {}
    with patch.object(scan.usb.core, "find", return_value=devices), \
         patch.object(scan, "_is_termux", return_value=False):
        scan.discover_usb(report=report)
    return report


def _out_only_iface(cls: int = 0x07) -> _FakeInterface:
    """A cheap label printer: bulk OUT, nothing coming back."""
    return _FakeInterface(cls, [_FakeEndpoint(0x03)])


def test_a_printer_with_no_bulk_in_is_reported_as_skipped():
    report = _report([_device(0x1234, 0x5678, _out_only_iface())])

    assert report["seen"] == 1 and report["matched"] == 0
    assert [d["id"] for d in report["skipped"]] == ["1234:5678"]
    assert report["skipped"][0]["reason"] == "no_bulk_in"
    assert report["skipped"][0]["message"]


def test_channels_split_across_interfaces_get_their_own_reason():
    """Found by review: asking whether the DEVICE has both channels answers a
    different question than the scan asks, which is whether ONE interface has
    both. Calling this a class mismatch would send support hunting the wrong
    thing."""
    report = _report([_device(
        0x1234, 0x5678,
        _FakeInterface(0x07, [_FakeEndpoint(0x81)]),
        _FakeInterface(0x07, [_FakeEndpoint(0x03)]),
    )])

    assert report["skipped"][0]["reason"] == "split_interfaces"


def test_a_device_whose_descriptors_cannot_be_read_is_reported():
    """What a printer held by another driver looks like — on macOS that is a
    printer someone added in System Settings."""
    class _Locked(_FakeDevice):
        def __iter__(self):
            raise OSError("access denied")

    report = _report([_Locked(0x0483, 0x5011, [])])

    assert report["skipped"][0]["reason"] == "descriptor_unreadable"
    assert report["skipped"][0]["id"] == "0483:5011"


def test_an_ordinary_device_is_not_reported_as_a_missing_printer():
    """A keyboard has no bulk OUT, so it cannot be a printer. Listing every
    hub and mouse would bury the one line support is looking for."""
    report = _report([_device(0x046D, 0xC534, _iface(0x03, with_bulk=False))])

    assert report["skipped"] == []


def test_a_printer_that_matched_is_not_reported_as_skipped():
    report = _report([_device(0x0519, 0x0001, _iface(0x07))])

    assert report["matched"] == 1 and report["skipped"] == []


def test_an_empty_bus_is_told_apart_from_a_skipped_printer():
    """Zero devices means libusb cannot reach the bus at all — a different
    problem from "saw it, walked past it", and the two used to look the same."""
    assert _report([]) == {"seen": 0, "matched": 0, "skipped": [], "skipped_total": 0}


def test_the_list_of_skipped_devices_is_capped():
    """A hub full of peripherals must not push the real answer out of sight."""
    many = [_device(0x1234, i, _out_only_iface()) for i in range(scan.MAX_REPORTED_SKIPS + 5)]

    report = _report(many)

    assert len(report["skipped"]) == scan.MAX_REPORTED_SKIPS
    assert report["skipped_total"] == scan.MAX_REPORTED_SKIPS + 5


def test_a_scan_that_cannot_start_does_not_look_like_a_clean_bus():
    """`usb.core.find` fails outright when there is no libusb backend. The
    report must say so rather than let a caller show the previous scan."""
    report: dict = {}
    with patch.object(scan.usb.core, "find", side_effect=RuntimeError("no backend")), \
         patch.object(scan, "_is_termux", return_value=False):
        scan.discover_all(usb_report=report)

    assert report["error"] == "no backend"
    assert report["seen"] == 0


def test_a_scan_that_never_runs_leaves_no_stale_numbers():
    """On Android the USB scan is skipped outright. A caller reusing a dict —
    or reading one filled by an earlier scan — must not be shown those numbers
    as if they described the bus right now."""
    report = {"seen": 7, "matched": 3, "skipped": [{"id": "dead:beef"}]}

    with patch.object(scan, "_is_termux", return_value=True):
        assert scan.discover_usb(report=report) == []

    assert report == {"seen": 0, "matched": 0, "skipped": [], "skipped_total": 0}
