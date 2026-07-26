"""Windows print-spooler transport (win32print RAW).

On Windows the manager prints USB thermal printers through the OS spooler —
the printer keeps its normal Windows driver (no WinUSB/Zadig, non-exclusive).
These tests exercise the platform-independent wiring with win32print mocked,
plus the worker's one-spool-doc-per-receipt open/close discipline.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from src.devices.printer import PrinterDevice
from src.devices.registry import PrinterRegistry
from src.models.printer import (
    PrinterDescriptor,
    PrinterRegistration,
    PrinterTransport,
    WindowsSpoolerAddress,
    make_id,
)


def test_discover_windows_printers_enumerates_spooler(monkeypatch):
    fake = types.ModuleType("win32print")
    fake.PRINTER_ENUM_LOCAL = 2
    fake.PRINTER_ENUM_CONNECTIONS = 4
    fake.EnumPrinters = lambda flags, name, level: [
        (0, "drv", "XP-58 Receipt", "comment"),
        (0, "drv", "Microsoft Print to PDF", "comment"),
    ]
    monkeypatch.setitem(sys.modules, "win32print", fake)

    from src.devices.scan import discover_windows_printers
    found = discover_windows_printers()

    names = [d.label for d in found]
    assert names == ["XP-58 Receipt", "Microsoft Print to PDF"]
    d = found[0]
    assert d.transport == PrinterTransport.windows_spooler.value
    assert d.windows.printer_name == "XP-58 Receipt"
    # id is stable for the same printer name
    assert d.id == make_id(PrinterTransport.windows_spooler, "XP-58 Receipt")


def test_discover_windows_printers_noop_without_win32(monkeypatch):
    # Simulate non-Windows: importing win32print fails → clean empty list.
    monkeypatch.setitem(sys.modules, "win32print", None)  # import → ImportError
    from src.devices.scan import discover_windows_printers
    assert discover_windows_printers() == []


def test_build_device_maps_spooler_cfg():
    desc = PrinterDescriptor(
        id="w1", transport=PrinterTransport.windows_spooler, label="XP-58",
        windows=WindowsSpoolerAddress(printer_name="XP-58"),
    )
    reg = PrinterRegistration(descriptor=desc, kind="receipt", paper_width=58)
    dev = PrinterRegistry._build_device(reg)
    assert dev._config["connection"] == "windows_spooler"
    assert dev._config["printer_name"] == "XP-58"
    assert dev._is_spooler() is True


class _FakeSpooler:
    """Stand-in for escpos.printer.Win32Raw — records the doc lifecycle."""

    def __init__(self):
        self.events: list[str] = []

    def open(self, job_name="python-escpos", raise_not_found=True):
        self.events.append("open")

    def close(self):
        self.events.append("close")


def test_worker_opens_and_closes_a_spool_doc_per_receipt():
    """Each print job must be wrapped in exactly one open()…close() so the
    receipt flushes as a single Windows spool document."""

    async def body():
        dev = PrinterDevice("spool", {
            "enabled": True,
            "paper_width": 58,
            "render_mode": "native",   # skip the bitmap patch for this unit test
            "code_page": None,
            "connection": "windows_spooler",
            "printer_name": "XP-58",
        })
        fake = _FakeSpooler()
        dev._printer = fake
        dev._worker_task = asyncio.create_task(dev._worker())

        async def job(printer):
            printer.events.append("print")

        await dev.enqueue(job)
        snapshot = list(fake.events)   # per-job lifecycle, before disconnect
        await dev.disconnect()
        return snapshot

    events = asyncio.run(asyncio.wait_for(body(), timeout=5))
    assert events == ["open", "print", "close"]


def test_worker_closes_doc_even_when_job_raises():
    async def body():
        dev = PrinterDevice("spool", {
            "enabled": True, "paper_width": 58, "render_mode": "native",
            "code_page": None, "connection": "windows_spooler",
            "printer_name": "XP-58",
        })
        fake = _FakeSpooler()
        dev._printer = fake
        dev._worker_task = asyncio.create_task(dev._worker())

        async def bad_job(printer):
            printer.events.append("print")
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await dev.enqueue(bad_job)
        snapshot = list(fake.events)   # before disconnect
        await dev.disconnect()
        return snapshot

    events = asyncio.run(asyncio.wait_for(body(), timeout=5))
    assert events == ["open", "print", "close"]  # closed despite the error
