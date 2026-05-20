"""PrivatBank adapter behaviour against a mock TCP terminal.

The mock server speaks PB's null-terminated JSON framing. Tests drive
real adapter coroutines through it — same approach as test_ssi_adapter
but with the PB wire format.

Coverage:
 - ping / get_info (identify)
 - list_merchants — index-dict parsing
 - charge happy path with fiscal adv parsing (natr + rid)
 - charge declined (responseCode >= 1000)
 - charge cancelled mid-flight (responseCode 1001)
 - charge with discount
 - refund by rrn
 - get_receipt_info
 - _format_amount edge cases
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
from src.services.terminals.privatbank import (
    DELIMITER,
    DELIMITER_BYTE,
    PrivatBankTerminalAdapter,
    _format_amount,
    _parse_adv,
    decode_frame,
    encode_frame,
)


# ---------------------------------------------------------------------------
# Mock PB terminal
# ---------------------------------------------------------------------------


class MockPBTerminal:
    """A scripted PB-protocol server. Per-method response queues like
    the SSI mock; reuses the last response if the queue runs dry so
    repeated calls don't blow up the test."""

    def __init__(self, responses: dict[str, list[dict]]) -> None:
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

    async def _handle(self, reader, writer) -> None:
        try:
            buf = bytearray()
            while True:
                chunk = await reader.read(1024)
                if not chunk:
                    return
                buf.extend(chunk)
                while True:
                    # Skip an optional leading delimiter (handshake).
                    start = 1 if buf[:1] == DELIMITER_BYTE else 0
                    idx = buf.find(DELIMITER, start)
                    if idx == -1:
                        break
                    frame = bytes(buf[: idx + 1])
                    del buf[: idx + 1]
                    request = decode_frame(frame)
                    self.requests.append(request)
                    response = self._next_response(request)
                    writer.write(encode_frame(response))
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return
        finally:
            writer.close()

    def _next_response(self, request: dict) -> dict:
        method = request.get("method", "")
        # ServiceMessage subroutes by msgType so tests can script
        # identify / getMerchantList / interrupt independently.
        if method == "ServiceMessage":
            msg_type = (request.get("params") or {}).get("msgType", "")
            key = f"ServiceMessage:{msg_type}"
        else:
            key = method
        bucket = self._responses.get(key) or self._responses.get(method, [])
        if not bucket:
            return {
                "method": method, "step": 0, "params": {},
                "error": True, "errorDescription": f"Unknown (mock): {key}",
            }
        if len(bucket) > 1:
            return bucket.pop(0)
        return bucket[0]


def _registration(port: int) -> TerminalRegistration:
    return TerminalRegistration(
        descriptor=TerminalDescriptor(
            id="pb-test",
            transport=TerminalTransport.network,
            label="PB mock",
            kind=TerminalKind.privat_pos,
            network=TerminalNetworkAddress(host="127.0.0.1", port=port),
        ),
        kind=TerminalKind.privat_pos,
        default_merchant_id="0",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_format_amount_handles_kopiks() -> None:
    assert _format_amount(60) == "0.60"
    assert _format_amount(0) == "0.00"
    assert _format_amount(100) == "1.00"
    assert _format_amount(24500) == "245.00"
    assert _format_amount(99) == "0.99"
    assert _format_amount(1) == "0.01"


def test_parse_adv_three_scenarios() -> None:
    # Bank e-чек only — natr absent, rid present
    assert _parse_adv('{"er":true,"rid":14725705133}') == (None, 14725705133)
    # National (fiscal) only — natr present, rid absent
    assert _parse_adv('{"natr":"h7v102902308142"}') == ("h7v102902308142", None)
    # Combined
    assert _parse_adv(
        '{"er":true,"natr":"h7v102902308142","rid":14725453137}'
    ) == ("h7v102902308142", 14725453137)


def test_parse_adv_returns_empty_for_non_json() -> None:
    # When fiscalization isn't active the `adv` field carries an
    # advertising string like "ПриватБанк" — we must NOT crash.
    assert _parse_adv("ПриватБанк") == (None, None)
    assert _parse_adv("") == (None, None)
    assert _parse_adv(None) == (None, None)
    assert _parse_adv("{bad json") == (None, None)


# ---------------------------------------------------------------------------
# Adapter methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_succeeds_against_mock() -> None:
    async with MockPBTerminal({
        "PingDevice": [{
            "method": "PingDevice", "step": 0,
            "params": {"code": "00", "responseCode": "0000"},
            "error": False, "errorDescription": "",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        assert await adapter.ping() is True


@pytest.mark.asyncio
async def test_ping_returns_false_when_offline() -> None:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    adapter = PrivatBankTerminalAdapter(_registration(port))
    assert await adapter.ping() is False


@pytest.mark.asyncio
async def test_get_info_returns_identify_params() -> None:
    async with MockPBTerminal({
        "ServiceMessage:identify": [{
            "method": "ServiceMessage", "step": 0,
            "params": {
                "msgType": "identify",
                "vendor": "PAX", "model": "A930",
                "serialNumber": "1490123",
            },
            "error": False, "errorDescription": "",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        info = await adapter.get_info()
        assert info["vendor"] == "PAX"
        assert info["serialNumber"] == "1490123"


@pytest.mark.asyncio
async def test_list_merchants_parses_index_dict() -> None:
    """Spec §6.4: response is index→label dict, e.g.
       {"3":"...","4":"...","msgType":"getMerchantList"}"""
    async with MockPBTerminal({
        "ServiceMessage:getMerchantList": [{
            "method": "ServiceMessage", "step": 0,
            "params": {
                "msgType": "getMerchantList",
                "3": "Оплата Частинами в періоді",
                "4": "Оплата частинами",
                "5": "Миттєва Розстрочка",
            },
            "error": False, "errorDescription": "",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        merchants = await adapter.list_merchants()
        by_id = {m.merchant_id: m.merchant_name for m in merchants}
        assert by_id == {
            "3": "Оплата Частинами в періоді",
            "4": "Оплата частинами",
            "5": "Миттєва Розстрочка",
        }


@pytest.mark.asyncio
async def test_charge_happy_path_with_fiscal_adv() -> None:
    """End-to-end Purchase with combined fiscal (Nat. чек + bank е-чек)
    in the adv field — what BarHandler will see for merchants with
    activated 'Каса' service."""
    async with MockPBTerminal({
        "Purchase": [{
            "method": "Purchase", "step": 0,
            "params": {
                "amount": "245.00",
                "approvalCode": "999999",
                "bankAcquirer": "ПриватБанк",
                "cardHolderName": "INSTANT/ISSUE",
                "invoiceNumber": "999999",
                "issuerName": "VISA ПРИВАТ",
                "merchant": "TSTTTTTT",
                "pan": "4731XXXXXXXX9838",
                "paymentSystem": "VISA",
                "posEntryMode": "022",
                "receipt": "text-of-receipt",
                "responseCode": "0000",
                "rrn": "9999999999999",
                "terminalId": "TSTSALE2",
                "trnStatus": "1",
                "txnType": "1",
                "adv": '{"er":true,"natr":"h7v102902308142","rid":14725453137}',
            },
            "error": False, "errorDescription": "",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        result = await adapter.charge(ChargeRequest(amount_kopecks=24500))

    assert result.status == "ok"
    assert result.rrn == "9999999999999"
    assert result.cardmask == "4731XXXXXXXX9838"
    assert result.paysys == "VISA"
    assert result.bank_name == "ПриватБанк"
    assert result.invoice_num == "999999"
    assert result.fiscal_receipt_id == "h7v102902308142"
    assert result.bank_receipt_id == 14725453137
    assert result.fiscal_receipt_text == "text-of-receipt"


@pytest.mark.asyncio
async def test_charge_request_amount_formatted_as_decimal() -> None:
    """Spec §5.1.1: amount must be "X.XX" decimal, never integer kopiks.
    24500 kopiks → "245.00" on the wire."""
    async with MockPBTerminal({
        "Purchase": [{
            "method": "Purchase", "step": 0,
            "params": {"responseCode": "0000", "trnStatus": "1"},
            "error": False, "errorDescription": "",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        await adapter.charge(ChargeRequest(amount_kopecks=24500))
        purchase = next(r for r in mock.requests if r.get("method") == "Purchase")
        assert purchase["params"]["amount"] == "245.00"
        assert purchase["step"] == 0   # int, not string


@pytest.mark.asyncio
async def test_charge_uses_default_merchant_when_none_provided() -> None:
    async with MockPBTerminal({
        "Purchase": [{
            "method": "Purchase", "step": 0,
            "params": {"responseCode": "0000", "trnStatus": "1"},
            "error": False, "errorDescription": "",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        await adapter.charge(ChargeRequest(amount_kopecks=100))
        purchase = next(r for r in mock.requests if r.get("method") == "Purchase")
        assert purchase["params"]["merchantId"] == "0"


@pytest.mark.asyncio
async def test_charge_declined_response_code() -> None:
    """responseCode >= 1000 → declined. Example: 1002 EMV Decline."""
    async with MockPBTerminal({
        "Purchase": [{
            "method": "Purchase", "step": 0,
            "params": {"responseCode": "1002"},
            "error": True, "errorDescription": "EMV Decline",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        result = await adapter.charge(ChargeRequest(amount_kopecks=100))
        assert result.status == "declined"
        assert result.response_code == "1002"
        assert result.error_code == "1002"
        assert result.error_message == "EMV Decline"


@pytest.mark.asyncio
async def test_charge_cancelled_via_interrupt_returns_1001() -> None:
    """Spec §6.2: when operator hits Cancel, the terminal aborts and
    returns the Purchase with responseCode 1001. Adapter maps this
    onto status='cancelled'."""
    async with MockPBTerminal({
        "Purchase": [{
            "method": "Purchase", "step": 0,
            "params": {"responseCode": "1001"},
            "error": True, "errorDescription": "Oперація скасов.",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        result = await adapter.charge(ChargeRequest(amount_kopecks=100))
        assert result.status == "cancelled"
        assert result.response_code == "1001"


@pytest.mark.asyncio
async def test_charge_with_discount() -> None:
    async with MockPBTerminal({
        "Purchase": [{
            "method": "Purchase", "step": 0,
            "params": {"responseCode": "0000", "trnStatus": "1"},
            "error": False, "errorDescription": "",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        await adapter.charge(ChargeRequest(
            amount_kopecks=24500,
            discounted_amount_kopecks=2200,
        ))
        purchase = next(r for r in mock.requests if r.get("method") == "Purchase")
        assert purchase["params"]["discount"] == "22.00"


@pytest.mark.asyncio
async def test_charge_unfiscalized_terminal_leaves_natr_empty() -> None:
    """Merchant without 'Каса' activated → `adv` is just an
    advertising string (or empty). natr/rid stay None — Reports
    treat this as 'fiscalization not active'."""
    async with MockPBTerminal({
        "Purchase": [{
            "method": "Purchase", "step": 0,
            "params": {
                "responseCode": "0000", "trnStatus": "1",
                "rrn": "111",
                "adv": "ПриватБанк",  # plain string, no JSON
            },
            "error": False, "errorDescription": "",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        result = await adapter.charge(ChargeRequest(amount_kopecks=100))
        assert result.status == "ok"
        assert result.fiscal_receipt_id is None
        assert result.bank_receipt_id is None


@pytest.mark.asyncio
async def test_refund_carries_rrn_in_request() -> None:
    """Spec §5.2.1: Refund requires the original rrn to reach back into
    earlier batches."""
    async with MockPBTerminal({
        "Refund": [{
            "method": "Refund", "step": 0,
            "params": {
                "responseCode": "0000", "trnStatus": "1",
                "rrn": "9999999999999",
                "txnType": "2",
            },
            "error": False, "errorDescription": "",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        result = await adapter.refund(
            ChargeRequest(amount_kopecks=12300),
            rrn="9999999999999",
        )
        refund = next(r for r in mock.requests if r.get("method") == "Refund")
        assert refund["params"]["rrn"] == "9999999999999"
        assert refund["params"]["amount"] == "123.00"
        assert result.status == "ok"


@pytest.mark.asyncio
async def test_get_receipt_info_returns_text_and_fiscal_id() -> None:
    """The recovery hook — look up a prior receipt by invoiceNumber.
    Returns the full receipt text + fiscal IDs."""
    async with MockPBTerminal({
        "GetReceiptInfo": [{
            "method": "GetReceiptInfo", "step": 0,
            "params": {
                "amount": "5.55",
                "approvalCode": "230719",
                "invoiceNumber": "000111",
                "pan": "XXXXXXXXXXXX6873",
                "receipt": "Чек № 000111",
                "responseCode": "0000",
                "rrn": "9999999",
                "adv": '{"er":true,"natr":"abc"}',
                "txnType": "1",
            },
            "error": False, "errorDescription": "",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        result = await adapter.get_receipt_info("000111")
        assert result.status == "ok"
        assert result.invoice_num == "000111"
        assert result.fiscal_receipt_id == "abc"
        assert result.fiscal_receipt_text == "Чек № 000111"


@pytest.mark.asyncio
async def test_cancel_swallows_errors() -> None:
    """Interrupt is best-effort — if terminal isn't idle for interrupt
    (or any other failure path), cancel() must NOT propagate."""
    async with MockPBTerminal({
        "ServiceMessage:interrupt": [{
            "method": "ServiceMessage", "step": 0,
            "params": {"msgType": "interrupt"},
            "error": True, "errorDescription": "Cannot interrupt now",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        await adapter.cancel()  # must not raise


@pytest.mark.asyncio
async def test_get_last_result_maps_zero_to_ok() -> None:
    """Spec §6.7: LastResult="0" → success, "2" → in-progress (we
    treat in-progress as declined for now; caller should retry or use
    GetReceiptInfo)."""
    async with MockPBTerminal({
        "ServiceMessage:getLastResult": [{
            "method": "ServiceMessage", "step": 0,
            "params": {"msgType": "getLastResult", "LastResult": "0"},
            "error": False, "errorDescription": "",
        }],
    }) as mock:
        adapter = PrivatBankTerminalAdapter(_registration(mock.port))
        result = await adapter.get_last_result()
        assert result.status == "ok"
        assert "LastResult=0" in (result.raw_transaction_result or "")
