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


class _FakeEndpoint:
    def __init__(self, address: int, attributes: int = 0x02) -> None:
        self.bEndpointAddress = address
        self.bmAttributes = attributes  # 0x02 == bulk


class _FakeInterface:
    def __init__(self, cls: int, endpoints: list[_FakeEndpoint]) -> None:
        self.bInterfaceClass = cls
        self._endpoints = endpoints

    def __iter__(self):
        return iter(self._endpoints)


class _FakeConfig:
    def __init__(self, interfaces: list[_FakeInterface]) -> None:
        self._interfaces = interfaces

    def __iter__(self):
        return iter(self._interfaces)


class _FakeDevice:
    def __init__(self, vendor: int, product: int, configs: list[_FakeConfig]) -> None:
        self.idVendor = vendor
        self.idProduct = product
        self._configs = configs
        # 0 → falsy → _safe_string returns None without touching libusb
        self.iManufacturer = 0
        self.iProduct = 0
        self.iSerialNumber = 0

    def __iter__(self):
        return iter(self._configs)


def _bulk_pair() -> list[_FakeEndpoint]:
    return [_FakeEndpoint(0x81), _FakeEndpoint(0x03)]  # bulk IN + bulk OUT


def _iface(cls: int, with_bulk: bool = True) -> _FakeInterface:
    return _FakeInterface(cls, _bulk_pair() if with_bulk else [])


def _device(vendor: int, product: int, *interfaces: _FakeInterface) -> _FakeDevice:
    return _FakeDevice(vendor, product, [_FakeConfig(list(interfaces))])


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
