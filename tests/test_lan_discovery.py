"""End-to-end LAN discovery against a real loopback SSI server.

A test pretends to be the operator's network: we stand up one
asyncio-based SSI server bound to 127.0.0.1, point the discovery
sweep at the smallest possible "subnet" (a list with just that one
host), and assert that `discover_network_terminals` returns a fully
enriched descriptor — model, serial, kind heuristic from the
package name. No mocks of `SSITerminalAdapter.probe` here; the wire
format and the discovery glue have to actually agree.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Iterator

import pytest

from src.devices.scan import SSI_TCP_PORT, discover_network_terminals
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
