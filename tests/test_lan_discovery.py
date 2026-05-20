"""End-to-end LAN discovery against real loopback POS terminals.

The test pretends to be the operator's network: we stand up an
asyncio-based mock terminal bound to 127.0.0.1, point the discovery
sweep at the smallest possible "subnet" (`127.0.0.1/32`), and assert
that `discover_network_terminals` returns a fully enriched descriptor
— model, serial, kind heuristic. No `.probe()` mocking — the wire
format and discovery glue have to agree end-to-end.

SSI and PrivatBank get separate loopback servers (different framing,
different ports). The combined-bank scenario verifies that one /24
sweep can pick up terminals on both protocols at once.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Iterator

import pytest

from src.devices.scan import PB_TCP_PORT, SSI_TCP_PORT, discover_network_terminals
from src.services.terminals.privatbank import (
    DELIMITER,
    DELIMITER_BYTE,
    decode_frame as pb_decode_frame,
    encode_frame as pb_encode_frame,
)
from src.services.terminals.ssi import decode_frame, encode_frame


class _LoopbackTerminal:
    """A blocking-thread SSI mock — owns its own event loop because
    `discover_network_terminals` calls `asyncio.run` and we can't have
    a running loop already there in the test thread."""

    def __init__(self, responses: dict, port: int) -> None:
        self._responses = {k: list(v) for k, v in responses.items()}
        self._port = port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.base_events.Server | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._server = self._loop.run_until_complete(
            asyncio.start_server(self._handle, "127.0.0.1", self._port),
        )
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header = await reader.readexactly(5)
                data_len = int.from_bytes(header[3:5], "big")
                rest = await reader.readexactly(data_len + 1)
                request = decode_frame(header + rest)
                method = request.get("method", "")
                bucket = self._responses.get(method, [])
                if bucket:
                    response = bucket.pop(0) if len(bucket) > 1 else bucket[0]
                else:
                    response = {"method": method, "error": True, "errorCode": "E05", "errorDescription": "Unknown method"}
                writer.write(encode_frame(response))
                await writer.drain()
        except asyncio.IncompleteReadError:
            return
        finally:
            writer.close()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self) -> None:
        if self._loop is not None and self._server is not None:
            self._loop.call_soon_threadsafe(self._server.close)
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)


@pytest.fixture
def loopback_terminal(unused_tcp_port: int) -> Iterator[int]:
    """Stand a fresh SSI mock on a free port. We can't bind 3000 in CI
    (privilege / collision) so the test monkey-patches `SSI_TCP_PORT`
    to point at whatever the OS gave us."""
    server = _LoopbackTerminal(
        responses={
            "PingDevice": [{"method": "PingDevice", "error": False, "errorCode": "", "errorDescription": "", "params": {}}],
            "GetTerminalInfo": [{
                "method": "GetTerminalInfo",
                "error": False, "errorCode": "", "errorDescription": "",
                "params": {
                    "terminalModel": "Verifone X990",
                    "terminalSerialNumber": "V1E0207420",
                    "currentApp": {"packageName": "com.paydustry.banking.monobank"},
                },
            }],
        },
        port=unused_tcp_port,
    )
    server.start()
    try:
        yield unused_tcp_port
    finally:
        server.stop()


def test_discover_finds_loopback_terminal(monkeypatch, loopback_terminal: int) -> None:
    """The full pipeline: TCP-connect sweep → SSI probe → descriptor
    with model + serial + kind heuristic from packageName."""
    import src.devices.scan as scan

    # Point the scan at our test port and at 127.0.0.1/32.
    monkeypatch.setattr(scan, "SSI_TCP_PORT", loopback_terminal)
    monkeypatch.setattr(
        scan,
        "_local_subnet",
        lambda: __import__("ipaddress").ip_network("127.0.0.1/32", strict=False),
    )

    descriptors = discover_network_terminals(timeout=1.0, probe_timeout=2.0)
    assert len(descriptors) == 1, f"expected 1 terminal, got {descriptors}"
    d = descriptors[0]
    assert d.model == "Verifone X990"
    assert d.serial == "V1E0207420"
    assert d.kind == "mono_pos"
    assert d.network.host == "127.0.0.1"
    assert d.network.port == loopback_terminal


def test_discover_returns_empty_when_subnet_unknown(monkeypatch) -> None:
    """No default route → empty list, never raises."""
    import src.devices.scan as scan

    monkeypatch.setattr(scan, "_local_subnet", lambda: None)
    assert discover_network_terminals() == []


# ---------------------------------------------------------------------------
# PrivatBank LAN-discovery
# ---------------------------------------------------------------------------


class _LoopbackPBTerminal:
    """PB-protocol counterpart of `_LoopbackTerminal`. Reads 0x00-
    terminated frames (tolerates an optional leading 0x00 for the
    handshake variant), writes the trailing-only response shape."""

    def __init__(self, responses: dict, port: int) -> None:
        self._responses = {k: list(v) for k, v in responses.items()}
        self._port = port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.base_events.Server | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._server = self._loop.run_until_complete(
            asyncio.start_server(self._handle, "127.0.0.1", self._port),
        )
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _handle(self, reader, writer) -> None:
        try:
            buf = bytearray()
            while True:
                chunk = await reader.read(1024)
                if not chunk:
                    return
                buf.extend(chunk)
                while True:
                    start = 1 if buf[:1] == DELIMITER_BYTE else 0
                    idx = buf.find(DELIMITER, start)
                    if idx == -1:
                        break
                    frame = bytes(buf[: idx + 1])
                    del buf[: idx + 1]
                    request = pb_decode_frame(frame)
                    method = request.get("method", "")
                    if method == "ServiceMessage":
                        msg_type = (request.get("params") or {}).get("msgType", "")
                        key = f"ServiceMessage:{msg_type}"
                    else:
                        key = method
                    bucket = self._responses.get(key, [])
                    if bucket:
                        response = bucket.pop(0) if len(bucket) > 1 else bucket[0]
                    else:
                        response = {
                            "method": method, "step": 0, "params": {},
                            "error": True, "errorDescription": "unknown",
                        }
                    writer.write(pb_encode_frame(response))
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return
        finally:
            writer.close()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self) -> None:
        if self._loop is not None and self._server is not None:
            self._loop.call_soon_threadsafe(self._server.close)
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)


@pytest.fixture
def loopback_pb_terminal(unused_tcp_port_factory) -> Iterator[int]:
    """PB-protocol loopback on a free port. We use a factory fixture so
    the combined-banks test below can stand both servers on different
    ports without colliding."""
    port = unused_tcp_port_factory()
    server = _LoopbackPBTerminal(
        responses={
            "PingDevice": [{
                "method": "PingDevice", "step": 0,
                "params": {"code": "00", "responseCode": "0000"},
                "error": False, "errorDescription": "",
            }],
            "ServiceMessage:identify": [{
                "method": "ServiceMessage", "step": 0,
                "params": {
                    "msgType": "identify",
                    "vendor": "PAX",
                    "model": "A930",
                    "serialNumber": "1490ABC",
                },
                "error": False, "errorDescription": "",
            }],
        },
        port=port,
    )
    server.start()
    try:
        yield port
    finally:
        server.stop()


def test_discover_finds_pb_terminal(monkeypatch, loopback_pb_terminal: int) -> None:
    """LAN sweep finds a PrivatBank terminal on port 2000 (patched to
    our test port) and decodes vendor/model/serial via identify."""
    import src.devices.scan as scan

    monkeypatch.setattr(scan, "PB_TCP_PORT", loopback_pb_terminal)
    monkeypatch.setattr(
        scan,
        "_local_subnet",
        lambda: __import__("ipaddress").ip_network("127.0.0.1/32", strict=False),
    )

    descriptors = discover_network_terminals(timeout=1.0, probe_timeout=2.0)
    assert len(descriptors) == 1, f"expected 1 PB terminal, got {descriptors}"
    d = descriptors[0]
    assert d.model == "A930"
    assert d.serial == "1490ABC"
    assert d.kind == "privat_pos"
    assert d.network.host == "127.0.0.1"
    assert d.network.port == loopback_pb_terminal


def test_discover_finds_both_ssi_and_pb_on_same_host(
    monkeypatch, unused_tcp_port_factory,
) -> None:
    """Single /24 sweep that finds an SSI terminal on one port and a PB
    terminal on another — both surface in one discovery result."""
    import src.devices.scan as scan

    ssi_port = unused_tcp_port_factory()
    pb_port = unused_tcp_port_factory()

    ssi_server = _LoopbackTerminal(
        responses={
            "PingDevice": [{"method": "PingDevice", "error": False, "errorCode": "", "errorDescription": "", "params": {}}],
            "GetTerminalInfo": [{
                "method": "GetTerminalInfo",
                "error": False, "errorCode": "", "errorDescription": "",
                "params": {
                    "terminalModel": "Verifone X990",
                    "terminalSerialNumber": "V1E0207420",
                    "currentApp": {"packageName": "com.paydustry.banking.monobank"},
                },
            }],
        },
        port=ssi_port,
    )
    pb_server = _LoopbackPBTerminal(
        responses={
            "PingDevice": [{
                "method": "PingDevice", "step": 0,
                "params": {"code": "00", "responseCode": "0000"},
                "error": False, "errorDescription": "",
            }],
            "ServiceMessage:identify": [{
                "method": "ServiceMessage", "step": 0,
                "params": {
                    "msgType": "identify",
                    "vendor": "PAX", "model": "A930",
                    "serialNumber": "PB-SN",
                },
                "error": False, "errorDescription": "",
            }],
        },
        port=pb_port,
    )
    ssi_server.start()
    pb_server.start()
    try:
        monkeypatch.setattr(scan, "SSI_TCP_PORT", ssi_port)
        monkeypatch.setattr(scan, "PB_TCP_PORT", pb_port)
        monkeypatch.setattr(
            scan,
            "_local_subnet",
            lambda: __import__("ipaddress").ip_network("127.0.0.1/32", strict=False),
        )

        descriptors = discover_network_terminals(timeout=1.0, probe_timeout=2.0)
    finally:
        ssi_server.stop()
        pb_server.stop()

    by_kind = {d.kind: d for d in descriptors}
    assert set(by_kind) == {"mono_pos", "privat_pos"}, (
        f"expected one of each kind, got {descriptors}"
    )
    assert by_kind["mono_pos"].serial == "V1E0207420"
    assert by_kind["privat_pos"].serial == "PB-SN"
