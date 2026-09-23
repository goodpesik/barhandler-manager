"""Stand-ins for pyusb devices, shared by the tests that drive the USB scan.

They live here rather than inside one test file so an end-to-end test can use
them without importing another test module's private helpers.
"""

from __future__ import annotations


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


