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
from escpos.printer import Dummy

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
    """Stand-in for escpos.printer.Win32Raw — records the doc lifecycle.

    `_raw` тут не для повноти: саме ним escpos пише БАЙТИ, і питання тесту —
    чи вони лягли ВСЕРЕДИНУ спул-документа. Доти джоб у тесті дописував
    «print» у перелік сам, тобто перевіряв власну заглушку, а не те, куди
    поїхав чек (BH-168).
    """

    def __init__(self):
        self.events: list[str] = []
        self.written: list[bytes] = []

    def open(self, job_name="python-escpos", raise_not_found=True):
        self.events.append("open")

    def _raw(self, data):
        self.events.append("print")
        self.written.append(bytes(data))

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
            printer._raw(b"RECEIPT")

        await dev.enqueue(job)
        snapshot = list(fake.events)   # per-job lifecycle, before disconnect
        await dev.disconnect()
        return snapshot

    events = asyncio.run(asyncio.wait_for(body(), timeout=5))
    assert events == ["open", "print", "close"]


def test_worker_never_opens_a_doc_for_a_job_that_failed():
    """Джоб упав — спул-документа не має бути взагалі.

    Доти перевірялось слабше: «документ закрито, попри помилку». З BH-168 байти
    складаються в памʼять і зливаються одним заходом ПІСЛЯ джоба, тож джоб, що
    впав, до заліза не доходить — порожнього завдання друку в черзі Windows не
    зʼявляється зовсім. Важливе тут те саме: після падіння не лишається
    відкритого документа.
    """
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
            printer._raw(b"HALF")
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await dev.enqueue(bad_job)
        snapshot = list(fake.events)   # before disconnect
        await dev.disconnect()
        return snapshot

    events = asyncio.run(asyncio.wait_for(body(), timeout=5))
    assert "open" not in events, "напівчек поїхав у спулер"
    assert events.count("close") <= 1, "документ відкрили й не закрили"


# ----- queue state: refuse a queue that cannot print, report a stuck job -----

_WORK_OFFLINE = 0x400
_PAUSED = 0x1


_OFFLINE = 0x80
_JOB_PRINTED = 0x80


def _fake_win32print(*, status=0, attributes=0, queued_jobs=(), job_status=0):
    """win32print stand-in for one queue «XP-58» on USB001."""
    fake = types.ModuleType("win32print")
    fake.PRINTER_ENUM_LOCAL = 2
    fake.PRINTER_ENUM_CONNECTIONS = 4
    fake.EnumPrinters = lambda flags, name, level: [(0, "drv", "XP-58", "")]
    fake.OpenPrinter = lambda name: object()
    fake.ClosePrinter = lambda handle: None
    fake.GetPrinter = lambda handle, level: {
        "pPortName": "USB001", "pDriverName": "Generic / Text Only",
        "Status": status, "Attributes": attributes, "cJobs": len(queued_jobs),
    }
    fake.EnumJobs = lambda handle, first, count, level: [
        {"JobId": job_id, "Status": job_status} for job_id in queued_jobs
    ]
    return fake


class _JobSpooler(Dummy):
    """Win32Raw stand-in with the full escpos API; sets `current_job` on open."""

    def __init__(self, job_id=7):
        super().__init__()
        self.events: list[str] = []
        self.job_id = job_id

    def open(self, job_name="python-escpos", raise_not_found=True):
        self.events.append("open")
        self.current_job = self.job_id

    def _raw(self, data):
        self.events.append("print")

    def close(self):
        self.events.append("close")


def _spooler_test_print(client, auth_headers, monkeypatch, win32print, job_id=7):
    from src.devices import scan, win_queue

    monkeypatch.setitem(sys.modules, "win32print", win32print)
    monkeypatch.setattr(win_queue, "DRAIN_WAIT_S", 0.3)
    desc = PrinterDescriptor(
        id=make_id(PrinterTransport.windows_spooler, "XP-58"),
        transport=PrinterTransport.windows_spooler, label="XP-58",
        windows=WindowsSpoolerAddress(printer_name="XP-58"),
    )
    monkeypatch.setattr(scan, "discover_network", lambda **_: [desc])
    client.post("/devices/discover", headers=auth_headers)
    r = client.post(
        "/devices/register", headers=auth_headers,
        json={"id": desc.id, "kind": "receipt", "paper_width": 58, "render_mode": "native"},
    )
    assert r.status_code == 200, r.text

    spooler = _JobSpooler(job_id)
    holder: dict = {}

    async def _device(_pid):
        if "dev" not in holder:
            dev = PrinterDevice("spool", {
                "enabled": True, "paper_width": 58, "render_mode": "native",
                "code_page": None, "connection": "windows_spooler",
                "printer_name": "XP-58",
            })
            dev._printer = spooler
            dev._worker_task = asyncio.create_task(dev._worker())
            holder["dev"] = dev
        return holder["dev"]

    monkeypatch.setattr(client.app.state.registry, "get_device", _device)
    return client.post(f"/devices/{desc.id}/test-print", headers=auth_headers), spooler


@pytest.mark.parametrize(
    ("queue", "code"),
    [
        ({"attributes": _WORK_OFFLINE}, "queue_offline"),
        ({"status": _PAUSED}, "queue_paused"),
        ({"status": _OFFLINE}, "queue_offline"),
    ],
)
def test_a_queue_that_cannot_print_is_refused_before_anything_is_sent(
    client, auth_headers, monkeypatch, queue, code,
):
    r, spooler = _spooler_test_print(
        client, auth_headers, monkeypatch, _fake_win32print(**queue),
    )

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == code
    assert "XP-58" in r.json()["detail"]["message"]
    assert spooler.events == []


def test_a_job_still_queued_after_the_wait_is_not_reported_as_printed(
    client, auth_headers, monkeypatch,
):
    r, spooler = _spooler_test_print(
        client, auth_headers, monkeypatch, _fake_win32print(queued_jobs=(5, 7)),
    )

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued_not_printed"
    assert "XP-58" in body["reason"]
    assert body["queue"]["port"] == "USB001"
    assert body["queue"]["jobs"] == 2
    assert (spooler.events[0], spooler.events[-1]) == ("open", "close")


def test_a_job_that_left_a_healthy_queue_is_printed(client, auth_headers, monkeypatch):
    # Job 5 belongs to somebody else; ours (7) has left the queue.
    r, _ = _spooler_test_print(
        client, auth_headers, monkeypatch, _fake_win32print(queued_jobs=(5,)),
    )

    assert r.status_code == 200
    assert r.json()["status"] == "printed"


@pytest.mark.parametrize("job_status", [_JOB_PRINTED, 0x100, 0x1000])  # PRINTED, DELETED, COMPLETE
def test_a_printed_job_kept_in_the_queue_counts_as_gone(client, auth_headers, monkeypatch, job_status):
    # "Keep printed documents": EnumJobs still lists our job, marked done.
    r, _ = _spooler_test_print(
        client, auth_headers, monkeypatch,
        _fake_win32print(queued_jobs=(7,), job_status=job_status),
    )

    assert r.status_code == 200
    assert r.json()["status"] == "printed"


def test_an_unknown_job_id_is_printed_without_waiting(client, auth_headers, monkeypatch):
    fake = _fake_win32print(queued_jobs=(5,))
    asked: list = []
    fake.EnumJobs = lambda *args: asked.append(args) or [{"JobId": 5, "Status": 0}]

    r, _ = _spooler_test_print(client, auth_headers, monkeypatch, fake, job_id=None)

    assert r.status_code == 200
    assert r.json()["status"] == "printed"
    assert asked == []


def test_a_receipt_on_an_offline_flagged_queue_makes_no_queue_read_and_is_sent(monkeypatch):
    fake = _fake_win32print(status=_OFFLINE)
    asked: list = []
    fake.OpenPrinter = lambda name: asked.append(name) or object()
    monkeypatch.setitem(sys.modules, "win32print", fake)

    async def body():
        dev = PrinterDevice("spool", {
            "enabled": True, "paper_width": 58, "render_mode": "native",
            "code_page": None, "connection": "windows_spooler",
            "printer_name": "XP-58",
        })
        spooler = _JobSpooler()
        dev._printer = spooler
        dev._worker_task = asyncio.create_task(dev._worker())

        async def job(printer):
            printer._raw(b"RECEIPT")

        await dev.enqueue(job)
        snapshot = list(spooler.events)   # before disconnect
        await dev.disconnect()
        return snapshot

    events = asyncio.run(asyncio.wait_for(body(), timeout=5))

    assert events == ["open", "print", "close"]
    assert asked == []


def test_discovery_shows_each_queue_port_driver_status_and_jobs(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "win32print",
        _fake_win32print(attributes=_WORK_OFFLINE, queued_jobs=(3,)),
    )
    from src.devices.scan import discover_windows_printers

    w = discover_windows_printers()[0].windows
    assert (w.port_name, w.driver_name, w.jobs) == ("USB001", "Generic / Text Only", 1)
    assert w.status_flags == ["work_offline"]


@pytest.mark.asyncio
async def test_spooler_queues_diagnostic_lists_every_queue(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32print", _fake_win32print(status=_PAUSED))
    from src.services.diagnostics import run_diagnostic

    r = await run_diagnostic("spooler_queues", {})
    assert r["ok"] is True
    assert r["queues"] == [{
        "name": "XP-58", "port": "USB001", "driver": "Generic / Text Only",
        "status": _PAUSED, "status_flags": ["paused"], "jobs": 0,
    }]
