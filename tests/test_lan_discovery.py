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

    # Scan ONLY our loopback SSI server: pin the host list to 127.0.0.1 and
    # the port→adapters map to just the SSI probe on our ephemeral port. This
    # keeps the test off the host's real /24 AND off the fixed bridge ports
    # (8080/8888/7777), which can otherwise collide with a locally running
    # emulator and yield an extra descriptor.
    import ipaddress
    loop = ipaddress.ip_network("127.0.0.1/32", strict=False)
    ssi_adapters = scan._terminal_port_adapters()[scan.SSI_TCP_PORT]
    monkeypatch.setattr(scan, "_local_subnet", lambda: loop)
    monkeypatch.setattr(scan, "_local_subnets", lambda: [loop])
    monkeypatch.setattr(
        scan, "_terminal_port_adapters", lambda: {loopback_terminal: ssi_adapters},
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
    """No default route → empty list, never raises.

    Both enumerators are pinned: the sweep reads `_local_subnets`, so
    leaving it live would have sent this test across the real network
    of whatever machine runs it.
    """
    import src.devices.scan as scan

    monkeypatch.setattr(scan, "_local_subnet", lambda: None)
    monkeypatch.setattr(scan, "_local_subnets", lambda: [])
    assert discover_network_terminals() == []


def test_discover_finds_a_terminal_slower_than_the_fast_pass(
    monkeypatch, loopback_terminal: int,
) -> None:
    """PET-1304 — a busy terminal can be slow to accept a connection.
    The sweep must come back for the addresses that stayed silent with
    more patience, or such a terminal is never discoverable at all."""
    import ipaddress

    import src.devices.scan as scan

    loop = ipaddress.ip_network("127.0.0.1/32", strict=False)
    ssi_adapters = scan._terminal_port_adapters()[scan.SSI_TCP_PORT]
    monkeypatch.setattr(scan, "_local_subnets", lambda: [loop])
    monkeypatch.setattr(
        scan, "_terminal_port_adapters", lambda: {loopback_terminal: ssi_adapters},
    )

    fast_ceiling = 0.3
    real_probe = scan._probe_tcp

    def refuses_the_fast_pass(host: str, port: int, timeout: float = 0.3) -> bool:
        if timeout <= fast_ceiling:
            return False
        return real_probe(host, port, timeout)

    monkeypatch.setattr(scan, "_probe_tcp", refuses_the_fast_pass)

    descriptors = discover_network_terminals(
        timeout=fast_ceiling, probe_timeout=2.0, slow_timeout=1.5,
    )
    assert len(descriptors) == 1, f"expected 1 terminal, got {descriptors}"
    assert descriptors[0].serial == "V1E0207420"


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

    # Scan ONLY our loopback PB server — see test_discover_finds_loopback_terminal
    # for why we also pin _terminal_port_adapters (avoids colliding with a
    # locally running emulator on 8080/8888/7777).
    import ipaddress
    loop = ipaddress.ip_network("127.0.0.1/32", strict=False)
    pb_adapters = scan._terminal_port_adapters()[scan.PB_TCP_PORT]
    monkeypatch.setattr(scan, "_local_subnet", lambda: loop)
    monkeypatch.setattr(scan, "_local_subnets", lambda: [loop])
    monkeypatch.setattr(
        scan, "_terminal_port_adapters", lambda: {loopback_pb_terminal: pb_adapters},
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
        import ipaddress
        loop = ipaddress.ip_network("127.0.0.1/32", strict=False)
        default_map = scan._terminal_port_adapters()
        monkeypatch.setattr(scan, "_local_subnet", lambda: loop)
        monkeypatch.setattr(scan, "_local_subnets", lambda: [loop])
        # Only our two ephemeral servers — not the real /24 or the fixed
        # bridge ports (which can collide with a running emulator).
        monkeypatch.setattr(scan, "_terminal_port_adapters", lambda: {
            ssi_port: default_map[scan.SSI_TCP_PORT],
            pb_port: default_map[scan.PB_TCP_PORT],
        })

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


def test_discover_always_tries_this_machine_itself(monkeypatch) -> None:
    """PET-1159 — a terminal on 127.0.0.1 could never be found.

    The loopback is filtered out of the local subnets (nobody else can reach
    it), so it never entered the host list — and a software terminal on the
    same machine as the till is exactly that address.
    """
    import src.devices.scan as scan

    probed: list[str] = []

    def record(host, port, timeout=0.3):
        probed.append(host)
        return False

    monkeypatch.setattr(scan, "_local_subnets", lambda: [])
    monkeypatch.setattr(scan, "_probe_tcp", record)

    assert discover_network_terminals(timeout=0.01) == []
    assert "127.0.0.1" in probed, f"the machine itself was not tried: {probed}"


def test_discovery_lists_a_terminal_bound_to_all_addresses_once(monkeypatch) -> None:
    """The same thing through the whole pipeline, not through the helper.

    A terminal bound to 0.0.0.0 answers on 127.0.0.1 AND on this machine's LAN
    address, which the sweep covers anyway — so the till used to see two
    terminals with different ids, and the one registered under one of them
    looked unknown under the other.
    """
    import ipaddress

    import src.devices.scan as scan
    from src.models.terminal import (
        TerminalDescriptor,
        TerminalKind,
        TerminalNetworkAddress,
        TerminalTransport,
    )

    monkeypatch.setattr(
        scan, "_local_subnets",
        lambda: [ipaddress.ip_network("192.168.0.0/30", strict=False)],
    )
    # Both addresses accept the connect on the SSI port, nothing else does.
    monkeypatch.setattr(
        scan, "_probe_tcp",
        lambda host, port, timeout=0.3: (
            port == scan.SSI_TCP_PORT and host in ("127.0.0.1", "192.168.0.1")
        ),
    )

    class OneTerminalEverywhere:
        """The same unit, whichever address it is reached at."""

        @staticmethod
        async def probe(host: str, port: int):
            return TerminalDescriptor(
                id=f"{host}-{port}",
                transport=TerminalTransport.network,
                label=f"terminal @ {host}",
                kind=TerminalKind.mono_pos,
                model="Verifone X990",
                serial="V1E0207420",
                network=TerminalNetworkAddress(host=host, port=port),
            )

    monkeypatch.setattr(
        scan, "_terminal_port_adapters",
        lambda: {scan.SSI_TCP_PORT: [OneTerminalEverywhere]},
    )

    found = discover_network_terminals(timeout=0.01)

    hosts = [d.network.host for d in found]
    assert hosts == ["192.168.0.1"], (
        f"one terminal, one entry, and at the address that works from "
        f"anywhere: {hosts}"
    )


def test_one_terminal_bound_to_all_addresses_is_listed_once() -> None:
    """PET-1159, found by the review — the loopback could double a terminal.

    A terminal (or its emulator) bound to 0.0.0.0 answers both on 127.0.0.1
    and on this machine's LAN address, which the sweep covers anyway. The id
    carries the host, so the till saw two terminals, and the one registered
    under one id looked unknown under the other.
    """
    from src.devices.scan import _without_loopback_twins
    from src.models.terminal import (
        TerminalDescriptor,
        TerminalKind,
        TerminalNetworkAddress,
        TerminalTransport,
    )

    def made(host: str, serial: str) -> TerminalDescriptor:
        return TerminalDescriptor(
            id=f"{host}:{serial}",
            transport=TerminalTransport.network,
            label=f"terminal @ {host}",
            kind=TerminalKind.mono_pos,
            model="Verifone X990",
            serial=serial,
            network=TerminalNetworkAddress(host=host, port=3000),
        )

    on_lan = made("192.168.0.42", "V1E0207420")
    on_loopback = made("127.0.0.1", "V1E0207420")
    another = made("127.0.0.1", "V1E9999999")

    kept = _without_loopback_twins([on_loopback, on_lan, another])

    hosts = sorted(d.network.host for d in kept)
    assert hosts == ["127.0.0.1", "192.168.0.42"], f"got {hosts}"
    # The twin that was dropped is the loopback copy of the LAN one...
    assert [d.serial for d in kept if d.network.host == "192.168.0.42"] == ["V1E0207420"]
    # ...and a terminal that answers ONLY on the loopback stays, which is the
    # whole point of scanning it.
    assert [d.serial for d in kept if d.network.host == "127.0.0.1"] == ["V1E9999999"]


def test_remote_discovery_answers_inside_the_server_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PET-1304 — the sweep got a second, patient pass and with it the
    ability to outlive the window the log server waits in: it gives up
    after `_SCAN_WAIT_S` and says «diagnostic timeout» while the scan
    goes on running on the till, and the operator's retry stacks
    another one on top. The remote command has to come back inside its
    budget and say that it did not cover everything."""
    import ipaddress
    import time

    import src.devices.scan as scan
    import src.services.diagnostics as diagnostics

    monkeypatch.setattr(
        scan, "_local_subnets",
        lambda: [ipaddress.ip_network("192.168.77.0/29")],
    )

    def nothing_ever_answers(host: str, port: int, timeout: float = 0.3) -> bool:
        # Every address silent, each connect costing its full timeout —
        # the worst case the budget exists for.
        time.sleep(timeout)
        return False

    monkeypatch.setattr(scan, "_probe_tcp", nothing_ever_answers)
    monkeypatch.setattr(diagnostics, "_SCAN_BUDGET_S", 3)

    started = time.monotonic()
    result = asyncio.run(diagnostics.run_diagnostic("terminal_discover", {}))
    elapsed = time.monotonic() - started

    assert result["ok"] is True
    assert elapsed < 3, f"answered after {elapsed:.1f}s, budget was 3s"
    # And it says so, instead of reporting a clean "no terminals here".
    assert result.get("partial") is True
    assert "second patient pass" in result["note"]
