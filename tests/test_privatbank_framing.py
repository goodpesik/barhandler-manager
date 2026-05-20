"""PrivatBank ECR JSON framing primitives.

Wire format from official spec 1.0.3.5 (sftp.privatbank.ua:2222 →
"ECR протокол ПриватБанк (JSON based) 1.0.3.5_integrator (ukr)_14012026.pdf"):

  Regular frame:  json_bytes + 0x00
  Handshake:      0x00 + json_bytes + 0x00   (first PingDevice only)

Tests cover the two encode variants, decode roundtrips, the
read-until-null helper, and the loopback probe-handshake against a
mock terminal speaking the bank's reference flow (PingDevice with
leading null → identify → connection closes between steps).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.services.terminals.privatbank import (
    DELIMITER,
    DELIMITER_BYTE,
    FrameError,
    PrivatBankTerminalAdapter,
    decode_frame,
    encode_frame,
)


# ---------------------------------------------------------------------------
# encode_frame
# ---------------------------------------------------------------------------


def test_encode_trailing_only_for_regular_frame() -> None:
    """Default shape — json bytes followed by one 0x00 terminator."""
    out = encode_frame({"method": "PingDevice", "step": 0})
    assert out.endswith(DELIMITER_BYTE)
    assert out[:1] != DELIMITER_BYTE  # no leading delimiter
    assert out.count(DELIMITER_BYTE) == 1


def test_encode_leading_and_trailing_for_handshake() -> None:
    """Handshake variant carries an EXTRA 0x00 at the start.
    Verified against the byte trace in spec §3:
        00 7b 22 6d 65 74 68 6f 64 22 ... 7d 00
    """
    out = encode_frame({"method": "PingDevice", "step": 0}, leading_delimiter=True)
    assert out[:1] == DELIMITER_BYTE
    assert out.endswith(DELIMITER_BYTE)
    assert out.count(DELIMITER_BYTE) == 2


def test_encode_handshake_byte_trace_matches_spec() -> None:
    """The spec §3 prints the literal bytes for the first PingDevice.
    Pin against that so we'd catch any drift in json.dumps output."""
    out = encode_frame({"method": "PingDevice", "step": 0}, leading_delimiter=True)
    expected = bytes([
        0x00,
        0x7b, 0x22, 0x6d, 0x65, 0x74, 0x68, 0x6f, 0x64, 0x22, 0x3a, 0x22, 0x50,
        0x69, 0x6e, 0x67, 0x44, 0x65, 0x76, 0x69, 0x63, 0x65, 0x22, 0x2c, 0x22,
        0x73, 0x74, 0x65, 0x70, 0x22, 0x3a, 0x30, 0x7d,
        0x00,
    ])
    assert out == expected


def test_encode_carries_utf8_cyrillic() -> None:
    """ensure_ascii=False in json.dumps — Ukrainian characters stay as
    UTF-8 multi-byte sequences, not as \\u-escapes."""
    out = encode_frame(
        {"method": "ServiceMessage", "step": 0,
         "params": {"msgType": "identify", "vendor": "ПриватБанк"}},
    )
    body = out[:-1]  # strip trailing 0x00
    assert "ПриватБанк".encode("utf-8") in body
    assert b"\\u041f" not in body  # not escape-encoded


def test_encode_rejects_oversized_payload() -> None:
    huge = {"method": "Purchase", "step": 0, "params": {"blob": "x" * 70_000}}
    with pytest.raises(ValueError, match="64K"):
        encode_frame(huge)


# ---------------------------------------------------------------------------
# decode_frame
# ---------------------------------------------------------------------------


def test_decode_roundtrip_simple() -> None:
    msg = {"method": "PingDevice", "step": 0}
    assert decode_frame(encode_frame(msg)) == msg


def test_decode_roundtrip_handshake_form() -> None:
    """Decoder tolerates a leading 0x00 (response that mirrors the
    handshake style — some terminals do)."""
    msg = {"method": "PingDevice", "step": 0,
           "params": {"code": "00", "responseCode": "0000"}}
    framed = encode_frame(msg, leading_delimiter=True)
    assert decode_frame(framed) == msg


def test_decode_roundtrip_cyrillic_purchase_response() -> None:
    """Realistic Purchase response shape from spec §5.1.2 — UTF-8
    Ukrainian in `bankAcquirer` and `adv` fields."""
    msg = {
        "method": "Purchase", "step": 0,
        "params": {
            "amount": "0.60",
            "responseCode": "0000",
            "bankAcquirer": "ПриватБанк",
            "adv": "{\"er\":true,\"natr\":\"h7v102902308142\",\"rid\":14725453137}",
        },
        "error": False, "errorDescription": "",
    }
    assert decode_frame(encode_frame(msg)) == msg


def test_decode_rejects_empty_buffer() -> None:
    with pytest.raises(FrameError, match="empty"):
        decode_frame(b"")


def test_decode_rejects_frame_without_terminator() -> None:
    body = b'{"method":"PingDevice","step":0}'  # no trailing 0x00
    with pytest.raises(FrameError, match="terminator"):
        decode_frame(body)


def test_decode_rejects_garbage_json() -> None:
    with pytest.raises(FrameError, match="JSON"):
        decode_frame(b"{not json}\x00")


def test_decode_rejects_empty_body_between_delimiters() -> None:
    """`\\x00\\x00` would be ambiguous — reject it instead of returning
    None/empty."""
    with pytest.raises(FrameError, match="empty"):
        decode_frame(DELIMITER_BYTE + DELIMITER_BYTE)


# ---------------------------------------------------------------------------
# Loopback handshake against a mock terminal
# ---------------------------------------------------------------------------


class _MockPBTerminal:
    """Tiny asyncio server that speaks PB framing. Scripted responses
    per method; reconnect between handshake steps is fine because the
    bank's reference flow does exactly that."""

    def __init__(self, responses: dict[str, dict]) -> None:
        self._responses = responses
        self.received_methods: list[str] = []
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        try:
            buf = bytearray()
            while True:
                chunk = await reader.read(1024)
                if not chunk:
                    return
                buf.extend(chunk)
                while True:
                    idx = buf.find(DELIMITER, 1 if buf[:1] == DELIMITER_BYTE else 0)
                    if idx == -1:
                        break
                    frame = bytes(buf[: idx + 1])
                    del buf[: idx + 1]
                    request = decode_frame(frame)
                    method = request.get("method", "")
                    self.received_methods.append(method)
                    response = self._responses.get(method, {
                        "method": method, "step": 0, "params": {},
                        "error": True, "errorDescription": "Unknown",
                    })
                    writer.write(encode_frame(response))
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return
        finally:
            writer.close()


@pytest.mark.asyncio
async def test_probe_completes_handshake_and_identify() -> None:
    """Full bank-reference probe flow: PingDevice (with leading null)
    + ServiceMessage(identify). On success, returns a descriptor
    populated with vendor / model / serial."""
    async with _MockPBTerminal({
        "PingDevice": {
            "method": "PingDevice", "step": 0,
            "params": {"code": "00", "responseCode": "0000"},
            "error": False, "errorDescription": "",
        },
        "ServiceMessage": {
            "method": "ServiceMessage", "step": 0,
            "params": {
                "msgType": "identify",
                "vendor": "PAX", "model": "A930",
                "serialNumber": "1490123456",
            },
            "error": False, "errorDescription": "",
        },
    }) as mock:
        descriptor = await PrivatBankTerminalAdapter.probe(
            "127.0.0.1", mock.port,
        )

    assert descriptor is not None
    assert descriptor.model == "A930"
    assert descriptor.serial == "1490123456"
    assert "PAX" in descriptor.label
    assert descriptor.network.host == "127.0.0.1"
    assert descriptor.network.port == mock.port


@pytest.mark.asyncio
async def test_probe_returns_descriptor_when_identify_fails() -> None:
    """Identify is best-effort — if the terminal accepts PingDevice
    but doesn't implement identify cleanly, we still return a usable
    descriptor (operator can edit model/serial later)."""
    async with _MockPBTerminal({
        "PingDevice": {
            "method": "PingDevice", "step": 0,
            "params": {"code": "00", "responseCode": "0000"},
            "error": False, "errorDescription": "",
        },
        # No ServiceMessage entry → returns error response
    }) as mock:
        descriptor = await PrivatBankTerminalAdapter.probe(
            "127.0.0.1", mock.port,
        )
    assert descriptor is not None
    assert descriptor.model is None
    assert descriptor.serial is None


@pytest.mark.asyncio
async def test_probe_returns_none_for_closed_port() -> None:
    """Discovery must never raise — a quiet host is just skipped."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    assert await PrivatBankTerminalAdapter.probe("127.0.0.1", port) is None


@pytest.mark.asyncio
async def test_first_request_carries_leading_delimiter_on_wire() -> None:
    """Bank's spec is unambiguous: the leading 0x00 only goes on the
    first PingDevice. Verify by sniffing the bytes the server sees."""
    seen: list[bytes] = []

    async def handle(reader, writer):
        chunk = await reader.read(1024)
        seen.append(bytes(chunk))
        writer.write(encode_frame({
            "method": "PingDevice", "step": 0,
            "params": {"code": "00", "responseCode": "0000"},
            "error": False, "errorDescription": "",
        }))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        from src.services.terminals.privatbank import send_tcp
        await send_tcp(
            "127.0.0.1", port,
            {"method": "PingDevice", "step": 0},
            leading_delimiter=True,
            timeout=2.0,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert seen, "server received nothing"
    assert seen[0][:1] == DELIMITER_BYTE, (
        f"first request must start with 0x00, got {seen[0][:4].hex()}"
    )
    assert seen[0][-1:] == DELIMITER_BYTE, "must end with 0x00 terminator"
