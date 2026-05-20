"""PrivatBank ECR JSON adapter — Ingenico / NEWLAND / PAX (NOT Verifone).

Wire protocol details (from official PrivatBank spec
"ECR протокол ПриватБанк (JSON based) 1.0.3.5_integrator (ukr)_14012026.pdf",
downloaded from sftp.privatbank.ua:2222 — public read-only creds in
the bank's own handbook page):

- Raw TCP on terminal port 2000 (newer Android terminals — PAX A930,
  NEWLAND N950, Ingenico Desk/Move with JSON firmware). Legacy
  terminals require a Windows / Linux helper (`genericDriverJson*`)
  that exposes a WebSocket on port 3000 — out of scope for this
  adapter; operator with a legacy device runs the helper next to the
  manager and registers the helper's host:port instead of the terminal.
- Framing: JSON UTF-8 body with a `0x00` null-byte TERMINATOR.
- Handshake quirk (spec §3): the FIRST PingDevice after a new connection
  carries an EXTRA leading `0x00` byte — total wire shape is
  `[0x00] + json + [0x00]`. Every subsequent frame is just
  `json + [0x00]`. Our adapter follows the bank's reference flow:
      connect → PingDevice (leading + trailing nulls) → disconnect →
      connect → ServiceMessage(identify) (trailing null only) →
      disconnect → connect → main session (keepalive).
- `step` is INT (0/1/2), NOT string ("1"/"2") like SSI ECR JSON.
- `amount` is DECIMAL STRING with "." or "," ("0.60" or "555,00"),
  NOT integer kopiks like SSI. We render `request.amount_kopecks / 100`
  with two fractional digits.

This adapter targets terminal-direct TCP. The HTTP/WebSocket variant
through the helper is a future addition (different transport, same
JSON payloads).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from src.models.terminal import (
    AcquirerResult,
    ChargeRequest,
    TerminalDescriptor,
    TerminalKind,
    TerminalNetworkAddress,
    TerminalRegistration,
    TerminalTransport,
    make_terminal_id,
)
from src.services.terminals.base import (
    MerchantInfo,
    TerminalAdapter,
    TerminalUnavailable,
)

logger = logging.getLogger(__name__)


DELIMITER = 0x00
DELIMITER_BYTE = bytes([DELIMITER])
DEFAULT_TCP_PORT = 2000        # newer terminals direct; helper-mode is 3000
REQUEST_TIMEOUT_S = 15.0       # spec §1.1 max per-request
INTER_REQUEST_PAUSE_S = 1.0    # spec §3 — 1s after PingDevice; safe general default
HANDSHAKE_PAUSE_S = 1.0        # 3-5s for Verifone per spec, 1s for others
MAX_FRAME_SIZE = 64 * 1024     # JSON ≤64K — same envelope guard as SSI

# trnStatus enum (Purchase 5.1.2)
_TRN_APPROVED = 1
_TRN_DECLINED = 2
_TRN_REVERSED = 3
_TRN_CANCELED = 4

# responseCode signals (Purchase 5.1.2 / spec intro)
_RESPONSE_OK = "0000"
# >=1000 → device-level error (1000 General, 1001 UserCanceled, 1002 EMVDecline,
#         1003 BatchFull, 1004 NoHostConn, 1005 NoPaper, 1006 KeysError,
#         1007 NoCardReader, 1008 TxnAlreadyComplete)
_RESPONSE_USER_CANCELED = "1001"


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------


class FrameError(ValueError):
    """Raised when a frame can't be decoded — corrupt JSON, missing
    terminator, payload too large. Bubbles up as TerminalUnavailable
    with `code="protocol_error"` at the transport layer."""


def encode_frame(message: dict, *, leading_delimiter: bool = False) -> bytes:
    """Serialize a dict as a PB ECR frame.

    Default shape: `json_bytes + 0x00`. Set `leading_delimiter=True`
    for the very first PingDevice on a new connection (spec §3).
    """
    # Compact separators — spec §3 byte trace has no whitespace, and
    # official C# / Go examples use compact form. Some terminals are
    # strict parsers (esp. legacy PAX firmware), so stay safe.
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME_SIZE:
        raise ValueError(f"PB frame exceeds 64K limit ({len(data)} bytes)")
    if leading_delimiter:
        return DELIMITER_BYTE + data + DELIMITER_BYTE
    return data + DELIMITER_BYTE


def decode_frame(buffer: bytes) -> dict:
    """Parse a single frame. Tolerates a leading 0x00 (handshake-style)
    and requires a trailing 0x00. Raises FrameError on malformed input.
    """
    if not buffer:
        raise FrameError("empty buffer")
    body = buffer
    if body[:1] == DELIMITER_BYTE:
        body = body[1:]
    if not body.endswith(DELIMITER_BYTE):
        raise FrameError("frame missing 0x00 terminator")
    body = body[:-1]
    if not body:
        raise FrameError("frame body empty between delimiters")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameError(f"invalid JSON in frame: {exc}") from exc


async def _read_until_null(reader: asyncio.StreamReader, timeout: float) -> bytes:
    """Read one PB frame (everything up to and including the trailing
    null). We don't know the length up front — JSON is variable-sized —
    so we just consume bytes until 0x00 appears. A leading 0x00 (if the
    peer copied the handshake style) is skipped.
    """
    buf = bytearray()
    leading_skipped = False
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        chunk = await asyncio.wait_for(reader.read(1024), timeout=remaining)
        if not chunk:
            raise asyncio.IncompleteReadError(bytes(buf), None)
        if not leading_skipped and chunk[:1] == DELIMITER_BYTE and not buf:
            # Skip a leading 0x00 if the terminal echoes one back.
            chunk = chunk[1:]
            leading_skipped = True
        buf.extend(chunk)
        idx = buf.find(DELIMITER)
        if idx != -1:
            return bytes(buf[: idx + 1])


async def send_tcp(
    host: str,
    port: int,
    message: dict,
    *,
    leading_delimiter: bool = False,
    timeout: float = REQUEST_TIMEOUT_S,
) -> dict:
    """Open a connection, send one framed message, read one framed
    response, close. Caller decides when to add the handshake leading-
    delimiter (only for the very first PingDevice after a fresh
    connect).
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise TerminalUnavailable(
            f"cannot connect to {host}:{port}: {exc}",
            code="unreachable",
        ) from exc

    try:
        writer.write(encode_frame(message, leading_delimiter=leading_delimiter))
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        raw = await _read_until_null(reader, timeout=timeout)
        return decode_frame(raw)
    except asyncio.TimeoutError as exc:
        raise TerminalUnavailable(
            f"timeout talking to {host}:{port}",
            code="timeout",
        ) from exc
    except (OSError, asyncio.IncompleteReadError) as exc:
        raise TerminalUnavailable(
            f"transport error talking to {host}:{port}: {exc}",
            code="unreachable",
        ) from exc
    except FrameError as exc:
        raise TerminalUnavailable(
            f"malformed PB response from {host}:{port}: {exc}",
            code="protocol_error",
        ) from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 — close may double-fault
            pass


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class PrivatBankTerminalAdapter(TerminalAdapter):
    """PrivatBank ECR JSON adapter — direct TCP to the terminal on port 2000.

    Bank-side activation: client must call PrivatBank support (3700 /
    0 800 500 030) to enable JSON protocol + port 2000 on their
    terminal. Without that, port 2000 is closed.

    Method bodies (charge, refund, etc.) land in commit 2. This commit
    delivers framing + handshake + ping so the LAN-scan path can probe
    terminals without dragging in the full method surface.
    """

    @classmethod
    async def probe(
        cls, host: str, port: int = DEFAULT_TCP_PORT,
    ) -> Optional[TerminalDescriptor]:
        """Bank's reference handshake (spec §3):
            1. connect → PingDevice (leading + trailing 0x00) → disconnect
            2. connect → ServiceMessage(identify) → disconnect
        Returns a descriptor populated with vendor + model from identify
        when both steps succeed. Returns None on any failure — discovery
        moves on quietly to the next host.
        """
        try:
            pong = await send_tcp(
                host, port,
                {"method": "PingDevice", "step": 0},
                leading_delimiter=True,
                timeout=2.5,
            )
        except (TerminalUnavailable, FrameError):
            return None
        if not isinstance(pong, dict) or pong.get("method") != "PingDevice":
            return None
        if pong.get("error") is True:
            return None

        await asyncio.sleep(HANDSHAKE_PAUSE_S)

        vendor: Optional[str] = None
        model: Optional[str] = None
        serial: Optional[str] = None
        try:
            ident = await send_tcp(
                host, port,
                {"method": "ServiceMessage", "step": 0,
                 "params": {"msgType": "identify"}},
                timeout=5.0,
            )
            params = ident.get("params", {}) if isinstance(ident, dict) else {}
            # identify shape varies slightly across vendors per spec §6.10
            vendor = params.get("vendor") or params.get("manufacturer")
            model = params.get("model") or params.get("terminalModel")
            serial = params.get("serialNumber") or params.get("terminalSerialNumber")
        except (TerminalUnavailable, FrameError):
            # Probe still useful even without identify — fall through.
            pass

        terminal_id = make_terminal_id(
            TerminalTransport.network,
            host,
            str(port),
            serial or "",
        )
        bits = [vendor, model] if (vendor or model) else ["PrivatBank POS"]
        label = f"{' '.join(b for b in bits if b)} @ {host}"
        if serial:
            label = f"{label} (s/n {serial})"

        return TerminalDescriptor(
            id=terminal_id,
            transport=TerminalTransport.network,
            label=label,
            kind=TerminalKind.privat_pos,
            model=model,
            serial=serial,
            network=TerminalNetworkAddress(host=host, port=port),
        )

    # ----- adapter API ------------------------------------------------

    def _addr(self) -> tuple[str, int]:
        net = self.descriptor.network
        if net is None:
            raise TerminalUnavailable(
                f"terminal {self.descriptor.id} has no network address",
                code="not_configured",
            )
        return net.host, net.port

    async def _send(
        self, message: dict, *, leading_delimiter: bool = False,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> dict:
        host, port = self._addr()
        return await send_tcp(
            host, port, message,
            leading_delimiter=leading_delimiter,
            timeout=timeout,
        )

    async def ping(self) -> bool:
        """Liveness check. Uses handshake-style leading delimiter on
        the assumption each call is a fresh connection (we don't hold
        long-lived sockets). Never raises."""
        try:
            response = await self._send(
                {"method": "PingDevice", "step": 0},
                leading_delimiter=True,
                timeout=3.0,
            )
        except TerminalUnavailable:
            return False
        return isinstance(response, dict) and response.get("error") is False

    async def get_info(self) -> dict:
        """Identify — section 6.10. Returns params dict verbatim."""
        response = await self._send(
            {"method": "ServiceMessage", "step": 0,
             "params": {"msgType": "identify"}},
            timeout=5.0,
        )
        _raise_if_error(response)
        return response.get("params") or {}

    async def list_merchants(self) -> list[MerchantInfo]:
        """ServiceMessage getMerchantList — spec §6.4.

        Response shape is INDEX-based dict, e.g.:
            {"3": "Оплата частинами", "4": "Розстрочка", ...}
        We turn that into MerchantInfo records where `merchant_id` is
        the index (the value Purchase actually expects in its
        `merchantId` param) and `merchant_name` is the human label.
        """
        response = await self._send(
            {"method": "ServiceMessage", "step": 0,
             "params": {"msgType": "getMerchantList"}},
            timeout=5.0,
        )
        _raise_if_error(response)
        params = response.get("params") or {}
        out: list[MerchantInfo] = []
        for key, value in params.items():
            if key == "msgType":
                continue
            # Numeric-string keys are merchant indexes; skip anything
            # that doesn't look like one so future spec additions
            # (extra metadata fields) don't break us.
            if not isinstance(key, str) or not key.isdigit():
                continue
            out.append(MerchantInfo(
                merchant_id=key,
                merchant_name=str(value) if value is not None else None,
            ))
        return out

    async def charge(self, request: ChargeRequest) -> AcquirerResult:
        """Synchronous Purchase per spec §5.1.

        Unlike SSI's ack-then-poll, PrivatBank's terminal accepts the
        Purchase and only returns once the whole chip/PIN/host round-
        trip is done. We just wait — the terminal's own timeout caps
        the wait. If the operator hits Cancel mid-flight, the parallel
        `cancel()` call lands an `interrupt`, and our Purchase response
        comes back with `responseCode: 1001`.
        """
        merchant_id = (
            request.merchant_id
            or self.registration.default_merchant_id
            or "0"   # Spec uses "0" as the default merchant index for
                     # single-merchant terminals (Torgsoft note: PAX has
                     # a bug on `1`, must use `0`).
        )
        params: dict = {
            "amount": _format_amount(request.amount_kopecks),
            "discount": (
                _format_amount(request.discounted_amount_kopecks)
                if request.discounted_amount_kopecks is not None
                else ""
            ),
            "merchantId": str(merchant_id),
        }
        for k, v in request.extras.items():
            params.setdefault(k, v)
        # Long timeout — cardholder may take 60s+ on chip+PIN flows.
        response = await self._send(
            {"method": "Purchase", "step": 0, "params": params},
            timeout=120.0,
        )
        return _result_from_purchase(response, transaction_uid=request.transaction_uid)

    async def cancel(self) -> None:
        """ServiceMessage interrupt — spec §6.2. Best-effort: the
        terminal echoes back `interruptTransmitted`, and the
        in-progress Purchase (on whoever's holding that socket) will
        finalize with `responseCode 1001`. We don't wait for the
        echo — the operator just needs the request fired."""
        try:
            await self._send(
                {"method": "ServiceMessage", "step": 0,
                 "params": {"msgType": "interrupt"}},
                timeout=3.0,
            )
        except TerminalUnavailable:
            logger.debug(
                "[%s] interrupt failed (likely already idle)",
                self.descriptor.id,
            )

    async def get_last_result(
        self, transaction_uid: Optional[str] = None,
    ) -> AcquirerResult:
        """For PrivatBank, `ServiceMessage getLastResult` doesn't carry
        the transaction details — only `LastResult: "0"|"2"` (BPOS-style
        success/in-progress). The actual receipt data lives in
        `GetReceiptInfo` by `invoiceNumber`. We fall back to a minimal
        AcquirerResult here; callers that need the full receipt should
        call `get_receipt_info()` directly with the invoice number.

        `transaction_uid` argument is accepted for ABC parity with SSI
        but not used — PB doesn't echo an external uid.
        """
        response = await self._send(
            {"method": "ServiceMessage", "step": 0,
             "params": {"msgType": "getLastResult"}},
            timeout=5.0,
        )
        _raise_if_error(response)
        params = response.get("params") or {}
        last_result = str(params.get("LastResult") or "")
        status = "ok" if last_result == "0" else "declined"
        return AcquirerResult(
            status=status,
            raw_transaction_result=f"LastResult={last_result}",
            vendor_data=params,
        )

    async def refund(self, request: ChargeRequest, *, rrn: str) -> AcquirerResult:
        """Reversal-by-RRN — spec §5.2. Refunds reach back into earlier
        batches (unlike Cancel which is current-batch-only), so RRN is
        the right key. Caller already has the RRN from the original
        Purchase result we returned."""
        merchant_id = (
            request.merchant_id
            or self.registration.default_merchant_id
            or "0"
        )
        params: dict = {
            "amount": _format_amount(request.amount_kopecks),
            "discount": (
                _format_amount(request.discounted_amount_kopecks)
                if request.discounted_amount_kopecks is not None
                else ""
            ),
            "merchantId": str(merchant_id),
            "rrn": rrn,
        }
        response = await self._send(
            {"method": "Refund", "step": 0, "params": params},
            timeout=120.0,
        )
        return _result_from_purchase(response)

    async def get_receipt_info(self, invoice_number: str) -> AcquirerResult:
        """Fetch a previously-completed receipt — spec §5.32. Returns
        full receipt text including the fiscal IDs in `adv` (so this is
        the right hook when the Purchase response got truncated or the
        operator asks 'where's my fiscal chek for invoice 999?').

        `invoice_number = "0"` or `""` → last receipt in batch.
        """
        response = await self._send(
            {"method": "GetReceiptInfo", "step": 0,
             "params": {"invoiceNumber": str(invoice_number)}},
            timeout=10.0,
        )
        # GetReceiptInfo's error envelope follows the same rules; not
        # raising here on `error:true` because the caller often wants
        # to surface "no such receipt" as a soft outcome.
        return _result_from_purchase(response)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _format_amount(kopecks: int) -> str:
    """Render an integer kopiks value into PB's decimal-string format.

    Spec §5.1.1 accepts "." or "," as the separator; we use "." for
    determinism (locale-free). Two fractional digits, no thousands sep.
    Negative values not allowed at this layer — request validators
    must filter them upstream.
    """
    grn, kop = divmod(int(kopecks), 100)
    return f"{grn}.{kop:02d}"


def _parse_adv(raw_adv: object) -> tuple[Optional[str], Optional[int]]:
    """Extract fiscal IDs from the `adv` field of a Purchase response.

    Per spec §5.1.2, when fiscalization is active `adv` is a JSON-
    encoded STRING containing one of three shapes:
        {"er": true, "rid": 14725705133}                  -- bank e-чек only
        {"natr": "h7v102902308142"}                       -- ДПС fiscal only
        {"er": true, "natr": "...", "rid": ...}           -- both
    For unfiscalized merchants the field is just an advertising
    string ("ПриватБанк") which won't parse — we return (None, None)
    silently.
    """
    if not isinstance(raw_adv, str) or not raw_adv:
        return None, None
    text = raw_adv.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None, None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None, None
    natr = parsed.get("natr") if isinstance(parsed, dict) else None
    rid = parsed.get("rid") if isinstance(parsed, dict) else None
    natr_str = str(natr) if natr is not None else None
    rid_int: Optional[int] = None
    if rid is not None:
        try:
            rid_int = int(rid)
        except (TypeError, ValueError):
            rid_int = None
    return natr_str, rid_int


def _result_from_purchase(
    response: dict, *, transaction_uid: Optional[str] = None,
) -> AcquirerResult:
    """Map a Purchase/Refund/GetReceiptInfo response onto the unified
    AcquirerResult shape. Status mapping uses both `trnStatus`
    (spec enum: 1=approved/2=declined/3=reversed/4=canceled) and
    `responseCode` (>=1000 → device error) so we catch both signals.
    """
    if not isinstance(response, dict):
        return AcquirerResult(
            status="declined",
            error_message=f"unexpected response shape: {type(response).__name__}",
        )
    params = response.get("params") or {}
    response_code = str(params.get("responseCode") or "")
    trn_status_raw = str(params.get("trnStatus") or "")
    error_flag = bool(response.get("error"))

    # Status priority: explicit "user canceled" (1001) > error envelope
    # > trnStatus enum > responseCode "0000".
    if response_code == _RESPONSE_USER_CANCELED:
        status = "cancelled"
    elif error_flag and response_code != _RESPONSE_OK:
        status = "declined"
    else:
        try:
            ts = int(trn_status_raw) if trn_status_raw else None
        except ValueError:
            ts = None
        if ts == _TRN_APPROVED and response_code == _RESPONSE_OK:
            status = "ok"
        elif ts in (_TRN_DECLINED, _TRN_REVERSED):
            status = "declined"
        elif ts == _TRN_CANCELED:
            status = "cancelled"
        elif response_code == _RESPONSE_OK:
            status = "ok"  # some methods (GetReceiptInfo) don't set trnStatus
        else:
            status = "declined"

    fiscal_id, bank_id = _parse_adv(params.get("adv"))

    return AcquirerResult(
        status=status,
        transaction_uid=transaction_uid,
        rrn=params.get("rrn"),
        auth_code=params.get("approvalCode"),
        cardmask=params.get("pan"),
        paysys=params.get("paymentSystem") or params.get("issuerName"),
        bank_name=params.get("bankAcquirer"),
        terminal_id=params.get("terminalId"),
        pos_entry_mode=params.get("posEntryMode"),
        invoice_num=params.get("invoiceNumber"),
        response_code=response_code or None,
        raw_transaction_result=trn_status_raw or None,
        error_code=response_code if response_code >= "1000" else None,
        error_message=response.get("errorDescription") or None,
        fiscal_receipt_id=fiscal_id,
        bank_receipt_id=bank_id,
        fiscal_receipt_text=params.get("receipt") or None,
        vendor_data=params,
    )


def _raise_if_error(response: dict, *, default_code: str = "error") -> None:
    """PrivatBank error envelope handling. Unlike SSI's `errorCode E00`
    enum, PB returns `responseCode` (string, "0000"=OK, >=1000=device
    error) inside `params` plus a top-level `error` boolean +
    `errorDescription`. We surface both signals as TerminalUnavailable
    with the responseCode (or the symbolic default) as `.code`."""
    if not isinstance(response, dict):
        raise TerminalUnavailable(
            f"unexpected response shape: {type(response).__name__}",
            code="protocol_error",
        )
    if response.get("error"):
        params = response.get("params") or {}
        code = (params.get("responseCode") or default_code).lower()
        message = (
            response.get("errorDescription")
            or params.get("responseCode")
            or "terminal returned error"
        )
        raise TerminalUnavailable(message, code=code)


__all__ = [
    "PrivatBankTerminalAdapter",
    "FrameError",
    "encode_frame",
    "decode_frame",
    "send_tcp",
    "DELIMITER",
    "DELIMITER_BYTE",
    "DEFAULT_TCP_PORT",
    "REQUEST_TIMEOUT_S",
    "INTER_REQUEST_PAUSE_S",
    "HANDSHAKE_PAUSE_S",
]
