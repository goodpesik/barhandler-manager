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
    RefundRequest,
    TerminalDescriptor,
    TerminalKind,
    TerminalNetworkAddress,
    TerminalRegistration,
    TerminalTransport,
)
from src.services.terminals.base import TerminalUnavailable
from src.services.terminals.privatbank import (
    CONNECT_ATTEMPTS,
    CONNECT_RETRY_PAUSE_S,
    CONNECT_TIMEOUT_S,
    DELIMITER,
    DELIMITER_BYTE,
    PrivatBankTerminalAdapter,
    _format_amount,
    _parse_adv,
    decode_frame,
    encode_frame,
    send_tcp,
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
            RefundRequest(amount_kopecks=12300, rrn="9999999999999"),
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


# ---------------------------------------------------------------------------
# Connect lifecycle (PET-1304)
#
# A shop's terminal answered 22 of 28 payments over two days and refused or
# timed out the other 5, recovering by itself within a minute. These cases
# pin the two properties that decide whether such a terminal costs a sale:
# a failed CONNECT is retried a bounded number of times, and the socket is
# always handed back. The seam is `asyncio.open_connection` — the network
# boundary — so the adapter under test runs for real.
# ---------------------------------------------------------------------------


class _FakeWriter:
    """Records what the adapter wrote and whether it was closed."""

    def __init__(self, fail_close: bool = False, fail_drain: bool = False) -> None:
        self.frames: list[bytes] = []
        self.closed = 0
        self._fail_close = fail_close
        self._fail_drain = fail_drain

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        if self._fail_drain:
            raise ConnectionResetError("drain failed")
        return None

    def close(self) -> None:
        self.closed += 1
        if self._fail_close:
            raise OSError("close failed")

    async def wait_closed(self) -> None:
        if self._fail_close:
            raise OSError("wait_closed failed")


class _FakeReader:
    """Replies with one scripted frame, or raises instead of replying.

    `raw` replaces the encoded reply with arbitrary bytes (a malformed
    frame). With neither `reply` nor `raw` the reader answers b"" —
    end of stream, which is how a terminal that hangs up mid-request
    looks to `_read_until_null`.
    """

    def __init__(
        self,
        reply: Optional[dict],
        raises: Optional[Exception] = None,
        raw: Optional[bytes] = None,
    ) -> None:
        if raw is not None:
            self._payload = raw
        else:
            self._payload = encode_frame(reply) if reply is not None else b""
        self._raises = raises
        self._sent = False

    async def read(self, _n: int) -> bytes:
        if self._raises is not None:
            raise self._raises
        if self._sent:
            return b""
        self._sent = True
        return self._payload


_APPROVED = {
    "method": "Purchase", "step": 0, "error": False, "errorDescription": "",
    "params": {"responseCode": "0000", "trnStatus": 1, "rrn": "024106595666"},
}


def _patch_connect(monkeypatch, outcomes: list) -> dict:
    """Script `asyncio.open_connection`: each entry is either an exception
    to raise or a (reader, writer) pair to hand back. `calls` counts the
    connect attempts the adapter actually made."""
    state: dict = {"calls": 0, "writers": []}

    async def fake_open_connection(host, port):
        i = state["calls"]
        state["calls"] += 1
        outcome = outcomes[i] if i < len(outcomes) else outcomes[-1]
        if isinstance(outcome, Exception):
            raise outcome
        state["writers"].append(outcome[1])
        return outcome

    monkeypatch.setattr(
        "src.services.terminals.privatbank.asyncio.open_connection",
        fake_open_connection,
    )
    monkeypatch.setattr(
        "src.services.terminals.privatbank.CONNECT_RETRY_PAUSE_S", 0,
    )
    return state


@pytest.mark.asyncio
async def test_charge_retries_a_refused_connect_and_still_pays(monkeypatch) -> None:
    """The terminal refused the first connect and answered the second.
    WinError 1225 came back in ~3 s on the till, so the retry is what
    turns that into a completed payment instead of a 503."""
    writer = _FakeWriter()
    state = _patch_connect(monkeypatch, [
        ConnectionRefusedError("[WinError 1225] refused"),
        (_FakeReader(_APPROVED), writer),
    ])
    adapter = PrivatBankTerminalAdapter(_registration(2000))

    result = await adapter.charge(ChargeRequest(amount_kopecks=24500))

    assert result.status == "ok"
    assert state["calls"] == 2


@pytest.mark.asyncio
async def test_charge_gives_up_after_a_bounded_number_of_connect_attempts(
    monkeypatch,
) -> None:
    """A terminal that is really off must not be retried for ever — the
    cashier is standing in front of the customer. Bounded from below (it did
    retry) and from above (what the worst case costs that cashier)."""
    state = _patch_connect(monkeypatch, [ConnectionRefusedError("refused")])
    adapter = PrivatBankTerminalAdapter(_registration(2000))

    with pytest.raises(TerminalUnavailable) as caught:
        await adapter.charge(ChargeRequest(amount_kopecks=24500))

    assert caught.value.code == "unreachable"
    # The literal, not the constant. Asserting against the constant imports
    # the very thing the case exists to pin: raising it to 10 would pass.
    assert state["calls"] == 3
    # And the bound from above, in the figure the cashier feels: three attempts
    # of the real connect budget with the real pause between them. The real
    # 4.0 / 1.0 / 3 give 14 s, and the whole argument for retrying a payment at
    # all is that the wait stays inside what someone will stand through.
    worst_case_s = (
        CONNECT_ATTEMPTS * CONNECT_TIMEOUT_S
        + (CONNECT_ATTEMPTS - 1) * CONNECT_RETRY_PAUSE_S
    )
    assert worst_case_s <= 14.0


@pytest.mark.asyncio
async def test_refund_retries_a_refused_connect_and_still_refunds(
    monkeypatch,
) -> None:
    """The refund opts into the retry for the same reason the charge does: the
    customer is standing at the till waiting for their money back, and a
    refused connect there costs the shop the same trip back."""
    writer = _FakeWriter()
    state = _patch_connect(monkeypatch, [
        ConnectionRefusedError("[WinError 1225] refused"),
        (_FakeReader(_APPROVED), writer),
    ])
    adapter = PrivatBankTerminalAdapter(_registration(2000))

    result = await adapter.refund(
        RefundRequest(amount_kopecks=24500, rrn="024106595666"),
    )

    assert result.status == "ok"
    assert state["calls"] == 2


def _post_connect_failure(kind: str) -> tuple["_FakeReader", "_FakeWriter"]:
    """A socket that connected and then failed, one way per `kind`.

    Each of these is a way the charge can fail AFTER the Purchase frame
    has been written, i.e. after the terminal may already be taking the
    money. The read timeout is the likeliest of them in the field: a
    charge allows the request 120 s for chip and PIN, and a terminal
    that goes away mid-transaction produces exactly that.
    """
    if kind == "read_reset":
        return _FakeReader(None, raises=ConnectionResetError("reset mid-request")), _FakeWriter()
    if kind == "read_timeout":
        # What `asyncio.wait_for` raises inside `_read_until_null` when the
        # request deadline runs out. Raised by the fake so the case costs
        # milliseconds instead of the real 120 s.
        return _FakeReader(None, raises=asyncio.TimeoutError()), _FakeWriter()
    if kind == "hung_up":
        # Reader at end of stream → asyncio.IncompleteReadError.
        return _FakeReader(None), _FakeWriter()
    if kind == "malformed_frame":
        # A complete frame (0x00-terminated) whose body is not JSON → FrameError.
        return _FakeReader(None, raw=b'{"method":"Purch\x00'), _FakeWriter()
    if kind == "error_reply":
        # The terminal ANSWERED, in a well-formed frame, that it will not do
        # the job — busy, or the card refused. Nothing failed on the transport,
        # so this is the one kind that comes back as a result instead of an
        # exception, and it is the kind a re-send would look most harmless on.
        return _FakeReader({
            "method": "Purchase", "step": 0, "error": True,
            "errorDescription": "terminal busy",
            "params": {"responseCode": "1000"},
        }), _FakeWriter()
    if kind == "drain_failed":
        # The write never reached the terminal — or reached it and the ACK
        # did not come back. Indistinguishable from here, so it counts as
        # "the Purchase may be live".
        return _FakeReader(_APPROVED), _FakeWriter(fail_drain=True)
    raise AssertionError(f"unknown failure kind {kind}")


@pytest.mark.parametrize(
    "kind",
    [
        "read_reset", "read_timeout", "hung_up", "malformed_frame",
        "drain_failed", "error_reply",
    ],
)
@pytest.mark.asyncio
async def test_a_purchase_is_never_resent_once_the_connect_succeeded(
    monkeypatch, kind: str,
) -> None:
    """The retry must cover the CONNECT only. Once a Purchase is on the
    wire the terminal may already be taking the money, so a request that
    fails after connecting is reported, never repeated — and that has to
    hold for EVERY way the request can fail, not just a reset, and not only
    the ways that fail loudly."""
    reader, writer = _post_connect_failure(kind)
    state = _patch_connect(monkeypatch, [(reader, writer)])
    adapter = PrivatBankTerminalAdapter(_registration(2000))

    if kind == "error_reply":
        # An answer, so it comes back as a refusal and not as an exception.
        result = await adapter.charge(ChargeRequest(amount_kopecks=24500))
        assert result.status == "declined"
    else:
        with pytest.raises(TerminalUnavailable):
            await adapter.charge(ChargeRequest(amount_kopecks=24500))

    assert state["calls"] == 1
    assert len(writer.frames) == 1


@pytest.mark.asyncio
async def test_socket_is_released_when_the_payment_succeeds(monkeypatch) -> None:
    writer = _FakeWriter()
    _patch_connect(monkeypatch, [(_FakeReader(_APPROVED), writer)])
    adapter = PrivatBankTerminalAdapter(_registration(2000))

    result = await adapter.charge(ChargeRequest(amount_kopecks=24500))

    assert result.status == "ok"
    assert writer.closed == 1


@pytest.mark.asyncio
async def test_socket_is_released_when_the_payment_fails(monkeypatch) -> None:
    writer = _FakeWriter()
    _patch_connect(monkeypatch, [
        (_FakeReader(None, raises=ConnectionResetError("reset")), writer),
    ])
    adapter = PrivatBankTerminalAdapter(_registration(2000))

    with pytest.raises(TerminalUnavailable):
        await adapter.charge(ChargeRequest(amount_kopecks=24500))

    assert writer.closed == 1


@pytest.mark.asyncio
async def test_a_failing_close_does_not_lose_the_payment_result(monkeypatch) -> None:
    """The release itself can throw on a half-dead socket. Losing the
    result of a payment the terminal already approved would be worse
    than the leak."""
    writer = _FakeWriter(fail_close=True)
    _patch_connect(monkeypatch, [(_FakeReader(_APPROVED), writer)])
    adapter = PrivatBankTerminalAdapter(_registration(2000))

    result = await adapter.charge(ChargeRequest(amount_kopecks=24500))

    assert result.status == "ok"
    assert writer.closed == 1


@pytest.mark.asyncio
async def test_a_connect_that_never_answers_is_cut_on_the_connect_budget(
    monkeypatch,
) -> None:
    """A charge gives the REQUEST 120 s because the cardholder needs it.
    The connect must not inherit that: on the till a dead terminal hung
    the connect for 21 s before Windows gave up, and under the request
    budget it could have held the cashier for two minutes."""
    monkeypatch.setattr(
        "src.services.terminals.privatbank.CONNECT_TIMEOUT_S", 0.05,
    )

    async def never_answers(host, port):
        await asyncio.sleep(30)

    monkeypatch.setattr(
        "src.services.terminals.privatbank.asyncio.open_connection", never_answers,
    )
    monkeypatch.setattr(
        "src.services.terminals.privatbank.CONNECT_RETRY_PAUSE_S", 0,
    )
    adapter = PrivatBankTerminalAdapter(_registration(2000))

    with pytest.raises(TerminalUnavailable) as caught:
        await asyncio.wait_for(
            adapter.charge(ChargeRequest(amount_kopecks=24500)), timeout=5,
        )

    assert caught.value.code == "unreachable"


@pytest.mark.asyncio
async def test_a_connect_says_how_long_the_endpoint_had_been_left_alone(
    monkeypatch, caplog,
) -> None:
    """What the field fault turns on is how long the terminal had been left
    alone before a connect failed, and the log is the only place that can
    answer it. So a connect that SUCCEEDS is on the record too, at a level
    production writes, carrying the gap since this process last closed a
    connection to the same endpoint — the first one has no previous close and
    says so, the second one measures it.
    """
    import logging as logging_module

    from src.services.terminals import privatbank as pb

    monkeypatch.setattr(pb, "_last_close_at", {})
    _patch_connect(monkeypatch, [
        (_FakeReader(_APPROVED), _FakeWriter()),
        (_FakeReader(_APPROVED), _FakeWriter()),
    ])
    adapter = PrivatBankTerminalAdapter(_registration(2000))

    with caplog.at_level(logging_module.INFO, logger=pb.__name__):
        await adapter.charge(ChargeRequest(amount_kopecks=24500))
        await adapter.charge(ChargeRequest(amount_kopecks=24500))

    connected = [
        r.getMessage() for r in caplog.records if ": connected on attempt" in r.getMessage()
    ]
    assert len(connected) == 2
    assert all(r.levelno == logging_module.INFO for r in caplog.records
               if ": connected on attempt" in r.getMessage())
    assert "last close of a connection to this endpoint: none in this process" \
        in connected[0]
    # The second connect follows a close, so the gap is a measured figure.
    assert "none in this process" not in connected[1]
    assert "last close of a connection to this endpoint: 0." in connected[1]


@pytest.mark.parametrize("call", ["ping", "probe", "merchants"])
@pytest.mark.asyncio
async def test_everything_but_a_payment_keeps_a_single_connect_attempt(
    monkeypatch, call: str,
) -> None:
    """The retry is for the two calls where the alternative is a lost sale.

    A liveness check has to report what is reachable RIGHT NOW. The discovery
    probe runs against every host of a /24 — retrying each silent address
    there turns one sweep into 254 x attempts x the connect budget and the
    scan stops finishing. And the merchant list is read when the cashier
    OPENS the payment window, before anybody has tried to pay: one attempt
    answers or refuses in up to 5 s, three would hold that window for up to
    14 s on every payment, in exchange for absorbing an outage that the
    observed ones were all longer than.
    """
    state = _patch_connect(monkeypatch, [ConnectionRefusedError("refused")])

    if call == "ping":
        adapter = PrivatBankTerminalAdapter(_registration(2000))
        assert await adapter.ping() is False
    elif call == "merchants":
        adapter = PrivatBankTerminalAdapter(_registration(2000))
        with pytest.raises(TerminalUnavailable):
            await adapter.list_merchants()
    else:
        assert await PrivatBankTerminalAdapter.probe("192.168.1.109", 2000) is None

    assert state["calls"] == 1


@pytest.mark.asyncio
async def test_a_caller_asking_for_less_than_the_connect_budget_gets_less(
    monkeypatch,
) -> None:
    """The connect budget is a CEILING, not a floor. `ping` asks for a
    3 s round trip while the budget is 4 s, so a connect handed the bare
    budget would outlive the deadline its own caller set. Here the caller
    asks for far less and the connect must be cut on the caller's figure,
    not on the budget."""

    async def never_answers(host, port):
        await asyncio.sleep(30)

    monkeypatch.setattr(
        "src.services.terminals.privatbank.asyncio.open_connection", never_answers,
    )

    # The fence is wide of the caller's 0.05 s and well inside the 4 s
    # connect budget, so it can only trip if the clamp is gone.
    with pytest.raises(TerminalUnavailable) as caught:
        await asyncio.wait_for(
            send_tcp(
                "192.168.1.109", 2000,
                {"method": "PingDevice", "step": 0},
                timeout=0.05,
                # Pinned so this case can only ever fail on the clamp and
                # not on someone changing how many attempts a connect gets.
                connect_attempts=1,
            ),
            timeout=1.0,
        )

    assert caught.value.code == "unreachable"
