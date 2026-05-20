"""SSI ECR JSON adapter — Monobank / PrivatBank / Raiffeisen / Pivdenny.

Wire protocol details (from SSI doc 1.4.1, indexed in MemPalace under
wing=barhandler, room=architecture):

- TCP socket on port 3000, frame layout:
      <STX 02 66 01> <LEN 2B big-endian> <DATA UTF-8 JSON ≤64K> <LRC 1B>
  LRC = XOR of every DATA byte.
- HTTP variant on port 3001 with plain JSON body (no framing) — we
  default to TCP because it's the broadly-supported transport; HTTP
  is opt-in via TerminalNetworkAddress.use_http.
- 15-second per-request timeout, ≥0.25s pause between requests.
- No auth — terminal trusted on LAN.

The protocol's async pattern is "ack-then-poll": every financial
operation returns `{error: false}` immediately and moves to a non-S00
status; the host polls `GetStatus` until S00, then reads the final
outcome via `GetLastResult`. We bake that loop into `charge()` so the
route layer just awaits a single coroutine.
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


STX_PREFIX = bytes([0x02, 0x66, 0x01])  # STX + protocol_id + version
DEFAULT_TCP_PORT = 3000
DEFAULT_HTTP_PORT = 3001
REQUEST_TIMEOUT_S = 15.0  # protocol max (sec 1.1)
INTER_REQUEST_PAUSE_S = 0.30  # > 0.25 protocol minimum
STATUS_POLL_INTERVAL_S = 0.5
STATUS_POLL_MAX_S = 180.0  # outer cap: cardholder can take a couple of minutes

# ESC/POS-style outcome mapping. APPROVED-anything → ok; explicit cancel →
# cancelled; everything else (decline / error / timeout) → declined and we
# pass the SSI verbatim value up to the UI so the operator sees specifics.
_OK_RESULTS = {"OK", "APPROVED", "APPROVED_ONLINE", "APPROVED_OFFLINE", "COMPLETED"}
_CANCELLED_RESULTS = {"CANCELLED", "CANCELLED_BEFORE_START"}
_INTERRUPTABLE_STATUSES = {"S02", "S03", "S08"}  # cancel() works in these only
_IDLE_STATUSES = {"S00", "S08"}  # charge complete + ready for next op


# ---------------------------------------------------------------------------
# Frame helpers (TCP transport)
# ---------------------------------------------------------------------------


def calc_lrc(payload: bytes) -> int:
    """XOR every byte of the DATA segment. Doc 6.1 algorithm verbatim."""
    lrc = 0
    for b in payload:
        lrc ^= b
    return lrc


def encode_frame(message: dict) -> bytes:
    """Build a complete TCP frame from a JSON-able dict.

    Doc §1.3 grammar: STX(3) + LEN(2 BE) + DATA + LRC(1). LEN counts
    only the DATA bytes — STX and LEN themselves are excluded.
    """
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(data) > 0xFFFF:
        raise ValueError(f"DATA exceeds 64K limit ({len(data)} bytes)")
    return STX_PREFIX + len(data).to_bytes(2, "big") + data + bytes([calc_lrc(data)])


class FrameError(ValueError):
    """Raised on STX mismatch or LRC failure — typically means the
    other end isn't speaking SSI, or the wire got corrupted."""


def decode_frame(buffer: bytes) -> dict:
    """Decode a complete frame. Raises FrameError on bad STX / LRC."""
    if len(buffer) < 6:
        raise FrameError(f"frame too short: {len(buffer)} bytes")
    if buffer[:3] != STX_PREFIX:
        raise FrameError(f"bad STX prefix: {buffer[:3].hex()}")
    data_len = int.from_bytes(buffer[3:5], "big")
    if len(buffer) < 5 + data_len + 1:
        raise FrameError(
            f"truncated frame: header says {data_len} data bytes, "
            f"got {len(buffer) - 6} after STX+LEN+LRC",
        )
    data = buffer[5 : 5 + data_len]
    received_lrc = buffer[5 + data_len]
    expected_lrc = calc_lrc(data)
    if received_lrc != expected_lrc:
        raise FrameError(
            f"LRC mismatch: got {received_lrc:#04x}, expected {expected_lrc:#04x}",
        )
    return json.loads(data.decode("utf-8"))


async def send_tcp(host: str, port: int, message: dict, timeout: float = REQUEST_TIMEOUT_S) -> dict:
    """Open a TCP connection, send one framed message, read the framed
    response, close. Caller is responsible for the ≥0.25s pause between
    consecutive calls."""
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
        writer.write(encode_frame(message))
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        # Header first so we know how many DATA bytes to read.
        header = await asyncio.wait_for(reader.readexactly(5), timeout=timeout)
        if header[:3] != STX_PREFIX:
            raise FrameError(f"bad STX in response: {header[:3].hex()}")
        data_len = int.from_bytes(header[3:5], "big")
        rest = await asyncio.wait_for(
            reader.readexactly(data_len + 1),  # +1 for LRC
            timeout=timeout,
        )
        return decode_frame(header + rest)
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
            f"malformed SSI response from {host}:{port}: {exc}",
            code="protocol_error",
        ) from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 — close may double-fault
            pass


# ---------------------------------------------------------------------------
# Adapter (probe-only in this commit; methods land in commit 2)
# ---------------------------------------------------------------------------


class SSITerminalAdapter(TerminalAdapter):
    """Implementation of TerminalAdapter for the SSI ECR JSON protocol.

    The class deliberately stays stateless beyond the registration so
    rerouting after a terminal reboot or DHCP change is a no-op — the
    registry rewrites the descriptor and the next charge() picks up
    the new address from `self.descriptor.network`.
    """

    @classmethod
    async def probe(cls, host: str, port: int = DEFAULT_TCP_PORT) -> Optional[TerminalDescriptor]:
        """Send PingDevice; if we get a valid SSI frame back, ask for
        GetTerminalInfo to flesh out the descriptor. On any transport
        or protocol error we return None so the LAN scan can move on
        quietly."""
        try:
            pong = await send_tcp(host, port, {"method": "PingDevice"}, timeout=2.0)
        except (TerminalUnavailable, FrameError):
            return None
        if not isinstance(pong, dict) or pong.get("method") != "PingDevice":
            return None

        # Best-effort: enrich with model + serial. If it fails we still
        # return a usable descriptor — operator can register and refine.
        model: Optional[str] = None
        serial: Optional[str] = None
        kind_hint: Optional[TerminalKind] = None
        try:
            info = await send_tcp(host, port, {"method": "GetTerminalInfo"}, timeout=5.0)
            params = info.get("params", {}) if isinstance(info, dict) else {}
            # Android shape: terminalModel + terminalSerialNumber + currentApp
            model = params.get("terminalModel") or params.get("model")
            serial = params.get("terminalSerialNumber") or params.get("pos_sn")
            current = params.get("currentApp") or {}
            package = (current.get("packageName") or "").lower() if isinstance(current, dict) else ""
            kind_hint = _kind_from_package(package)
        except (TerminalUnavailable, FrameError):
            pass

        terminal_id = make_terminal_id(
            TerminalTransport.network,
            host,
            str(port),
            serial or "",
        )
        label = f"{model or 'SSI POS'} @ {host}"
        if serial:
            label = f"{label} (s/n {serial})"

        return TerminalDescriptor(
            id=terminal_id,
            transport=TerminalTransport.network,
            label=label,
            kind=kind_hint,
            model=model,
            serial=serial,
            network=TerminalNetworkAddress(host=host, port=port),
        )

    # ----- adapter methods --------------------------------------------

    def _addr(self) -> tuple[str, int]:
        net = self.descriptor.network
        if net is None:
            raise TerminalUnavailable(
                f"terminal {self.descriptor.id} has no network address",
                code="not_configured",
            )
        return net.host, net.port

    async def _send(self, message: dict, timeout: float = REQUEST_TIMEOUT_S) -> dict:
        host, port = self._addr()
        return await send_tcp(host, port, message, timeout=timeout)

    async def ping(self) -> bool:
        """Quick liveness — never raises."""
        try:
            response = await self._send({"method": "PingDevice"}, timeout=3.0)
        except TerminalUnavailable:
            return False
        return isinstance(response, dict) and response.get("error") is False

    async def get_info(self) -> dict:
        """Full GetTerminalInfo. Caller deals with the Android/Linux
        shape divergence by checking for `terminalModel` vs `model`."""
        response = await self._send({"method": "GetTerminalInfo"}, timeout=5.0)
        _raise_if_error(response)
        return response.get("params") or {}

    async def list_merchants(self) -> list[MerchantInfo]:
        """GetMerchantListDetailed — single-merchant terminals return
        a one-element list which the frontend renders as a disabled
        select."""
        response = await self._send({"method": "GetMerchantListDetailed"}, timeout=5.0)
        _raise_if_error(response)
        merchants_raw = (response.get("params") or {}).get("merchantList") or []
        out: list[MerchantInfo] = []
        for entry in merchants_raw:
            if not isinstance(entry, dict):
                continue
            out.append(
                MerchantInfo(
                    merchant_id=str(entry.get("merchantId") or ""),
                    terminal_id=entry.get("terminalId"),
                    merchant_name=entry.get("merchantName"),
                ),
            )
        return out

    async def charge(self, request: ChargeRequest) -> AcquirerResult:
        """End-to-end Purchase flow per doc §3.1:

            1. send Purchase, terminal acks {error: false}
            2. poll GetStatus until terminal returns to S00 or S08
            3. fetch final outcome via GetLastResult (or GetResultByUid
               when we have an external uid)

        Times out after STATUS_POLL_MAX_S (3 minutes) and Interrupts —
        a runaway transaction never blocks the manager event loop.
        """
        merchant_id = (
            request.merchant_id
            or self.registration.default_merchant_id
            or ""
        )
        if not merchant_id:
            raise TerminalUnavailable(
                "no merchant_id provided and registration has no default",
                code="missing_merchant",
            )

        params: dict = {
            "transAmount": str(request.amount_kopecks),
            "transCurrency": request.currency,
            "merchantId": merchant_id,
        }
        if request.transaction_uid:
            params["transactionUid"] = request.transaction_uid
        if request.discounted_amount_kopecks is not None:
            params["discountedAmount"] = str(request.discounted_amount_kopecks)
        if self.registration.default_terminal_id:
            params["terminalId"] = self.registration.default_terminal_id
        for k, v in request.extras.items():
            params.setdefault(k, v)

        ack = await self._send(
            {"method": "Purchase", "step": "1", "params": params},
        )
        _raise_if_error(ack, default_code="charge_rejected")

        # The terminal is now busy. Poll status, interrupting after the
        # overall window so a stalled cashier interaction can't block
        # the event loop indefinitely.
        await self._wait_idle()

        return await self.get_last_result(
            transaction_uid=request.transaction_uid,
        )

    async def _wait_idle(self) -> None:
        """Poll GetStatus until S00 / S08 (idle for next op). Honours
        the protocol's ≥0.25s inter-request pause and the doc's 15s
        per-request timeout; gives up after STATUS_POLL_MAX_S and tries
        an Interrupt so we don't leave the terminal locked."""
        deadline = asyncio.get_running_loop().time() + STATUS_POLL_MAX_S
        while True:
            await asyncio.sleep(INTER_REQUEST_PAUSE_S)
            status_response = await self._send({"method": "GetStatus"}, timeout=5.0)
            status = (
                status_response.get("status")
                or (status_response.get("params") or {}).get("status")
            )
            if status in _IDLE_STATUSES:
                return
            if asyncio.get_running_loop().time() > deadline:
                # Best-effort cancel and surface as timeout — the
                # caller maps this onto a "cancelled" AcquirerResult.
                with _suppress(TerminalUnavailable):
                    await self._send({"method": "Interrupt"}, timeout=3.0)
                raise TerminalUnavailable(
                    f"terminal stayed busy >{STATUS_POLL_MAX_S}s "
                    f"(last status: {status})",
                    code="timeout",
                )
            await asyncio.sleep(STATUS_POLL_INTERVAL_S - INTER_REQUEST_PAUSE_S)

    async def cancel(self) -> None:
        """Interrupt — only effective in S02/S03/S08 per doc §5.4.2."""
        try:
            await self._send({"method": "Interrupt"}, timeout=3.0)
        except TerminalUnavailable:
            # If the terminal is unreachable or the operation already
            # finished there's nothing actionable here; we log and let
            # the caller decide whether to retry the surrounding flow.
            logger.debug(
                "[%s] cancel/Interrupt failed (likely already idle)",
                self.descriptor.id,
            )

    async def get_last_result(
        self, transaction_uid: Optional[str] = None,
    ) -> AcquirerResult:
        """Fetch the final outcome of the most recent operation. Uses
        GetResultByUid when the caller has a transaction_uid (more
        precise — survives a second operation between the failure and
        the recovery call); otherwise GetLastResult."""
        if transaction_uid:
            request: dict = {
                "method": "GetResultByUid",
                "params": {"transactionUid": transaction_uid},
            }
        else:
            request = {"method": "GetLastResult"}
        response = await self._send(request, timeout=5.0)
        _raise_if_error(response, default_code="result_fetch_failed")
        return _result_from_params(response.get("params") or {})


def _raise_if_error(response: dict, *, default_code: str = "error") -> None:
    """Standard SSI error envelope handling — convert any `error: true`
    response into TerminalUnavailable with the SSI code as `.code`."""
    if not isinstance(response, dict):
        raise TerminalUnavailable(
            f"unexpected response shape: {type(response).__name__}",
            code="protocol_error",
        )
    if response.get("error"):
        code = (response.get("errorCode") or default_code).lower()
        message = (
            response.get("errorDescription")
            or response.get("errorCode")
            or "terminal returned error"
        )
        raise TerminalUnavailable(message, code=code)


def _result_from_params(params: dict) -> AcquirerResult:
    """Map SSI GetLastResult / GetResultByUid params onto the unified
    AcquirerResult shape. Unknown enum values pass through verbatim in
    `raw_transaction_result` so we don't lose information."""
    raw = (params.get("transactionResult") or "").upper().replace("-", "_")
    if raw in _OK_RESULTS:
        status = "ok"
    elif raw in _CANCELLED_RESULTS:
        status = "cancelled"
    else:
        status = "declined"
    return AcquirerResult(
        status=status,
        transaction_uid=params.get("transactionUid"),
        rrn=params.get("rrn"),
        auth_code=params.get("authCode"),
        cardmask=params.get("pan"),
        paysys=params.get("binName"),
        bank_name=params.get("bankName"),
        terminal_id=params.get("terminalId"),
        pos_entry_mode=params.get("posEntryMode"),
        invoice_num=params.get("invoiceNum"),
        response_code=params.get("responseCode"),
        raw_transaction_result=raw or None,
        error_code=params.get("errorCode") or None,
        error_message=params.get("errorDescription") or None,
        error_details=params.get("errorDetails") or None,
        vendor_data=params,
    )


class _suppress:  # tiny stand-in for contextlib.suppress in async land
    def __init__(self, *exc_types):
        self._exc_types = exc_types

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, self._exc_types)


def _kind_from_package(package_name: str) -> Optional[TerminalKind]:
    """Heuristic: which bank does the active payment app belong to.

    Used during probe to label discovered terminals so the operator
    doesn't have to guess. Falls through to `generic_ssi` for unknown
    packages.
    """
    if "monobank" in package_name:
        return TerminalKind.mono_pos
    if "privat" in package_name:
        return TerminalKind.privat_pos
    if "raif" in package_name:
        return TerminalKind.raif_pos
    if "pivdenny" in package_name:
        return TerminalKind.pivdenny_pos
    return None


__all__ = [
    "SSITerminalAdapter",
    "FrameError",
    "calc_lrc",
    "encode_frame",
    "decode_frame",
    "send_tcp",
    "STX_PREFIX",
    "DEFAULT_TCP_PORT",
    "REQUEST_TIMEOUT_S",
    "INTER_REQUEST_PAUSE_S",
]
