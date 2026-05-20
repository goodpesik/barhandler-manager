"""SSI adapter behaviour against a mock TCP terminal.

The mock server is a tiny asyncio.start_server that speaks the SSI
framing the real terminal does — receives one framed request, looks up
a scripted response by method name, returns it. Letting the adapter
talk to a loopback socket gives us coverage that's one wire-format
mistake away from real-terminal behaviour without a real device.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import pytest

from src.models.terminal import (
    ChargeRequest,
    TerminalDescriptor,
    TerminalKind,
    TerminalNetworkAddress,
    TerminalRegistration,
    TerminalTransport,
)
from src.services.terminals.base import TerminalUnavailable
from src.services.terminals.ssi import (
    SSITerminalAdapter,
    STX_PREFIX,
    decode_frame,
    encode_frame,
)


# ---------------------------------------------------------------------
# Mock terminal server
# ---------------------------------------------------------------------


class MockTerminal:
    """A scripted SSI server. Pass `responses` mapping method-name →
    list of dict responses; each method-call pops the next one. Tests
    that drive a full Purchase flow stack GetStatus responses (S01,
    S01, S00) then a GetLastResult."""

    def __init__(self, responses: dict) -> None:
        # `responses[method]` is consumed in order; if exhausted we
        # reuse the last entry so a long status-poll doesn't run out.
        self._responses = {k: list(v) for k, v in responses.items()}
        self.requests: list[dict] = []
        self._server: Optional[asyncio.base_events.Server] = None
        self.port = 0

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header = await reader.readexactly(5)
                data_len = int.from_bytes(header[3:5], "big")
                rest = await reader.readexactly(data_len + 1)
                request = decode_frame(header + rest)
                self.requests.append(request)
                response = self._next_response(request.get("method", ""))
                writer.write(encode_frame(response))
                await writer.drain()
        except asyncio.IncompleteReadError:
            return  # client closed
        finally:
            writer.close()

    def _next_response(self, method: str) -> dict:
        bucket = self._responses.get(method, [])
        if not bucket:
            return {
                "method": method,
                "error": True,
                "errorCode": "E05",
                "errorDescription": f"Unknown method (mock): {method}",
            }
        # Last response sticks — convenient for status polls.
        if len(bucket) > 1:
            return bucket.pop(0)
        return bucket[0]


def _registration(port: int, kind: TerminalKind = TerminalKind.mono_pos) -> TerminalRegistration:
    return TerminalRegistration(
        descriptor=TerminalDescriptor(
            id="test",
            transport=TerminalTransport.network,
            label="mock",
            kind=kind,
            network=TerminalNetworkAddress(host="127.0.0.1", port=port),
        ),
        kind=kind,
        default_merchant_id="000000060007176",
    )


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_succeeds_against_mock() -> None:
    async with MockTerminal({
        "PingDevice": [{"method": "PingDevice", "error": False, "errorCode": "", "errorDescription": "", "params": {}}],
    }) as mock:
        adapter = SSITerminalAdapter(_registration(mock.port))
        assert await adapter.ping() is True


@pytest.mark.asyncio
async def test_ping_returns_false_when_terminal_offline() -> None:
    # Bind a server then close it — the next connect fails.
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()

    adapter = SSITerminalAdapter(_registration(port))
    assert await adapter.ping() is False  # never raises — quiet failure


@pytest.mark.asyncio
async def test_list_merchants_parses_detailed_response() -> None:
    async with MockTerminal({
        "GetMerchantListDetailed": [{
            "method": "GetMerchantListDetailed",
            "error": False, "errorCode": "", "errorDescription": "",
            "params": {
                "merchantCount": 2,
                "merchantList": [
                    {"merchantId": "M1", "terminalId": "T1", "merchantName": "ФОП Левинець"},
                    {"merchantId": "M2", "terminalId": "T1", "merchantName": "ТОВ Smile Bar"},
                ],
            },
        }],
    }) as mock:
        adapter = SSITerminalAdapter(_registration(mock.port))
        merchants = await adapter.list_merchants()
        assert [m.merchant_id for m in merchants] == ["M1", "M2"]
        assert merchants[0].merchant_name == "ФОП Левинець"


@pytest.mark.asyncio
async def test_charge_full_flow_returns_approved() -> None:
    """End-to-end happy path:
        Purchase ack → GetStatus S01 → GetStatus S00 → GetLastResult APPROVED
    """
    async with MockTerminal({
        "Purchase": [{"method": "Purchase", "error": False, "errorCode": "", "errorDescription": "", "params": {}}],
        "GetStatus": [
            {"method": "GetStatus", "error": False, "errorCode": "", "errorDescription": "", "status": "S01", "params": {}},
            {"method": "GetStatus", "error": False, "errorCode": "", "errorDescription": "", "status": "S00", "params": {}},
        ],
        "GetResultByUid": [{
            "method": "GetResultByUid",
            "error": False, "errorCode": "", "errorDescription": "",
            "params": {
                "originalTrnName": "Purchase",
                "transAmount": "24500",
                "transactionResult": "APPROVED-ONLINE",
                "rrn": "9999999999999",
                "authCode": "123456",
                "pan": "4725XXXXXXXX1627",
                "binName": "VISA",
                "bankName": "Bank Acquirer",
                "terminalId": "T0000001",
                "posEntryMode": "CONTACTLESS",
                "invoiceNum": "999999",
                "transactionUid": "external-uid-1",
            },
        }],
    }) as mock:
        adapter = SSITerminalAdapter(_registration(mock.port))
        result = await adapter.charge(ChargeRequest(
            amount_kopecks=24500,
            transaction_uid="external-uid-1",
        ))
        assert result.status == "ok"
        assert result.rrn == "9999999999999"
        assert result.cardmask == "4725XXXXXXXX1627"
        assert result.transaction_uid == "external-uid-1"
        assert result.raw_transaction_result == "APPROVED_ONLINE"


@pytest.mark.asyncio
async def test_charge_maps_declined_result() -> None:
    async with MockTerminal({
        "Purchase": [{"method": "Purchase", "error": False, "errorCode": "", "errorDescription": "", "params": {}}],
        "GetStatus": [{"method": "GetStatus", "error": False, "errorCode": "", "errorDescription": "", "status": "S00", "params": {}}],
        "GetLastResult": [{
            "method": "GetLastResult",
            "error": False, "errorCode": "", "errorDescription": "",
            "params": {
                "transactionResult": "DECLINED-ONLINE",
                "responseCode": "05",
                "errorDescription": "Do not honor",
            },
        }],
    }) as mock:
        adapter = SSITerminalAdapter(_registration(mock.port))
        result = await adapter.charge(ChargeRequest(amount_kopecks=100))
        assert result.status == "declined"
        assert result.raw_transaction_result == "DECLINED_ONLINE"
        assert result.response_code == "05"


@pytest.mark.asyncio
async def test_charge_maps_cancelled_result() -> None:
    async with MockTerminal({
        "Purchase": [{"method": "Purchase", "error": False, "errorCode": "", "errorDescription": "", "params": {}}],
        "GetStatus": [{"method": "GetStatus", "error": False, "errorCode": "", "errorDescription": "", "status": "S00", "params": {}}],
        "GetLastResult": [{
            "method": "GetLastResult",
            "error": False, "errorCode": "", "errorDescription": "",
            "params": {"transactionResult": "CANCELLED_BEFORE_START"},
        }],
    }) as mock:
        adapter = SSITerminalAdapter(_registration(mock.port))
        result = await adapter.charge(ChargeRequest(amount_kopecks=100))
        assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_charge_request_carries_required_fields() -> None:
    """Verify the SSI Purchase request payload matches doc §5.2.1.1
    when our ChargeRequest is filled."""
    async with MockTerminal({
        "Purchase": [{"method": "Purchase", "error": False, "errorCode": "", "errorDescription": "", "params": {}}],
        "GetStatus": [{"method": "GetStatus", "error": False, "errorCode": "", "errorDescription": "", "status": "S00", "params": {}}],
        "GetResultByUid": [{
            "method": "GetResultByUid", "error": False, "errorCode": "", "errorDescription": "",
            "params": {"transactionResult": "APPROVED"},
        }],
    }) as mock:
        adapter = SSITerminalAdapter(_registration(mock.port))
        await adapter.charge(ChargeRequest(
            amount_kopecks=24500,
            currency="980",
            transaction_uid="abc-123",
            discounted_amount_kopecks=22300,
        ))
        purchase = next(r for r in mock.requests if r.get("method") == "Purchase")
        assert purchase["step"] == "1"
        params = purchase["params"]
        assert params["transAmount"] == "24500"
        assert params["transCurrency"] == "980"
        assert params["merchantId"] == "000000060007176"
        assert params["transactionUid"] == "abc-123"
        assert params["discountedAmount"] == "22300"


@pytest.mark.asyncio
async def test_charge_raises_when_no_merchant_id() -> None:
    """Registration without a default_merchant_id + request without
    one in the body → fast fail before we touch the network."""
    reg = TerminalRegistration(
        descriptor=TerminalDescriptor(
            id="test",
            transport=TerminalTransport.network,
            label="mock",
            kind=TerminalKind.mono_pos,
            network=TerminalNetworkAddress(host="127.0.0.1", port=1),
        ),
        kind=TerminalKind.mono_pos,
    )
    adapter = SSITerminalAdapter(reg)
    with pytest.raises(TerminalUnavailable) as exc:
        await adapter.charge(ChargeRequest(amount_kopecks=100))
    assert exc.value.code == "missing_merchant"


@pytest.mark.asyncio
async def test_charge_surfaces_terminal_busy_error() -> None:
    """SSI E06 'Terminal Busy' on Purchase ack → adapter raises
    TerminalUnavailable(code='e06') so the route returns 503 with
    the SSI code the frontend can branch on."""
    async with MockTerminal({
        "Purchase": [{
            "method": "Purchase",
            "error": True, "errorCode": "E06",
            "errorDescription": "Terminal Busy",
            "params": {},
        }],
    }) as mock:
        adapter = SSITerminalAdapter(_registration(mock.port))
        with pytest.raises(TerminalUnavailable) as exc:
            await adapter.charge(ChargeRequest(amount_kopecks=100))
        assert exc.value.code == "e06"


@pytest.mark.asyncio
async def test_cancel_does_not_raise_when_terminal_already_idle() -> None:
    """Interrupt outside S02/S03/S08 returns E08 — adapter must not
    propagate; cancel is best-effort."""
    async with MockTerminal({
        "Interrupt": [{
            "method": "Interrupt",
            "error": True, "errorCode": "E08",
            "errorDescription": "Interrupt prohibited",
            "params": {},
        }],
    }) as mock:
        adapter = SSITerminalAdapter(_registration(mock.port))
        await adapter.cancel()  # must NOT raise


@pytest.mark.asyncio
async def test_get_last_result_by_uid_uses_correct_method() -> None:
    """When transaction_uid is provided we must call GetResultByUid,
    not GetLastResult — different buffers on the terminal side."""
    async with MockTerminal({
        "GetResultByUid": [{
            "method": "GetResultByUid",
            "error": False, "errorCode": "", "errorDescription": "",
            "params": {"transactionResult": "APPROVED", "transactionUid": "u-42"},
        }],
    }) as mock:
        adapter = SSITerminalAdapter(_registration(mock.port))
        result = await adapter.get_last_result(transaction_uid="u-42")
        assert result.status == "ok"
        assert result.transaction_uid == "u-42"
        assert mock.requests[0]["method"] == "GetResultByUid"
        assert mock.requests[0]["params"]["transactionUid"] == "u-42"


@pytest.mark.asyncio
async def test_probe_returns_descriptor_for_responsive_terminal() -> None:
    """The probe (used by LAN discovery) sends PingDevice then
    GetTerminalInfo and synthesises a TerminalDescriptor."""
    async with MockTerminal({
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
    }) as mock:
        descriptor = await SSITerminalAdapter.probe("127.0.0.1", mock.port)
        assert descriptor is not None
        assert descriptor.model == "Verifone X990"
        assert descriptor.serial == "V1E0207420"
        assert descriptor.kind == TerminalKind.mono_pos.value


@pytest.mark.asyncio
async def test_probe_returns_none_for_closed_port() -> None:
    """Probe must never raise on a quiet host — LAN discovery scans
    a whole /24 and a per-host failure should just skip it."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    assert await SSITerminalAdapter.probe("127.0.0.1", port) is None
