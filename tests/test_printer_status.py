"""Status-query contract: out-of-paper / cover-open / offline surface
as 503 with structured `detail` codes the frontend can branch on."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.constants import DEFAULT_API_KEY
from src.devices.printer import PrinterDevice, PrinterUnavailable
from src.server import create_app


def test_check_status_handles_unsupported_printer() -> None:
    """Cheap clones don't answer the status query. We must NOT crash —
    we report `supported: False` and let the print proceed."""
    device = PrinterDevice("test", {"enabled": True})
    fake_printer = MagicMock()
    fake_printer.paper_status.side_effect = Exception("not supported")
    device._printer = fake_printer

    status = device.check_status()
    assert status["supported"] is False
    assert status["online"] is True  # be lenient — proceed by default


def test_check_status_reports_empty_paper() -> None:
    device = PrinterDevice("test", {"enabled": True})
    fake_printer = MagicMock()
    fake_printer.paper_status.return_value = 0  # 0 = empty
    fake_printer.is_online.return_value = True
    device._printer = fake_printer

    status = device.check_status()
    assert status == {"online": True, "paper": "empty", "supported": True}


def test_check_status_reports_low_paper() -> None:
    device = PrinterDevice("test", {"enabled": True})
    fake_printer = MagicMock()
    fake_printer.paper_status.return_value = 1
    fake_printer.is_online.return_value = True
    device._printer = fake_printer
    assert device.check_status()["paper"] == "low"


def test_check_status_when_disconnected() -> None:
    device = PrinterDevice("test", {"enabled": True})
    # _printer remains None — emulates the "manager booted, hardware
    # unplugged" case.
    assert device.check_status() == {
        "online": False,
        "paper": "unknown",
        "supported": False,
    }


def test_printer_unavailable_carries_code() -> None:
    exc = PrinterUnavailable("paper out", code="out_of_paper")
    assert exc.code == "out_of_paper"
    assert str(exc) == "paper out"


def test_printer_unavailable_defaults_to_unavailable() -> None:
    exc = PrinterUnavailable("generic")
    assert exc.code == "unavailable"


@pytest.mark.asyncio
async def test_a_failing_close_does_not_report_a_reachable_printer_offline(
    monkeypatch,
) -> None:
    """`async_probe` opens a throwaway connection to decide whether a
    network printer is still there, and the heartbeat drops the handle
    when it answers False. The connect succeeding IS the answer: a
    `close()` that throws on a half-dead socket must not turn a reachable
    printer into an offline one."""
    import asyncio

    from src.devices import printer as printer_module

    class _ThrowingWriter:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1
            raise OSError("close failed")

        async def wait_closed(self) -> None:
            raise OSError("wait_closed failed")

    writer = _ThrowingWriter()

    async def fake_open_connection(host, port):
        return object(), writer

    monkeypatch.setattr(
        printer_module.asyncio, "open_connection", fake_open_connection,
    )

    device = PrinterDevice(
        "net", {"connection": "network", "host": "192.168.1.70", "port": 9100},
    )
    device._printer = MagicMock()

    assert await device.async_probe() is True
    assert writer.closed == 1
