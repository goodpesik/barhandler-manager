"""BPOS adapter — Bank Pivdenny (BPOS1) / Sense Bank (BPOS Light).

Pivdenny and Sense drive Ingenico terminals through the **BPOS** ECR
protocol. On Windows this is historically an ActiveX library
(`ECR_ActiveX_Library`); to reach it cross-platform the operator runs a
small BPOS bridge/host next to the manager that re-exposes the same
commands over TCP. Over the wire it's length-prefixed JSON, and BPOS uses
**numeric command codes** and a `result` status digit (0=approved) — the
same "BPOS-style 0/2" convention PrivatBank's `getLastResult` echoes.

The official BPOS1 spec is partner-gated (see the terminal-protocol
research note), so the command numbers and field names below are our
best-effort model, kept byte-consistent with `emulator/bpos_terminal.py`.
Real doc values are marked `# SPEC:`.

Wire model (provisional):
    Framing : 4-byte big-endian length prefix + JSON body.
    Port    : 8888 (BPOS bridge default).                             # SPEC:
    Amount  : decimal string "12.34" (like PrivatBank), not kopecks.  # SPEC:
    Purchase: single request/response (bridge blocks until done).
Commands  : "0" status/echo, "1" purchase, "3" refund, "9" abort,
            "80" merchant list, "90" get info, "95" last result.      # SPEC:
"""

from __future__ import annotations

import asyncio
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
from src.services.terminals.bridge import (
    MAX_FRAME_SIZE,
    BridgeFrameError,
    exchange,
    json_bytes,
)

logger = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 8888          # SPEC: BPOS bridge default
REQUEST_TIMEOUT_S = 15.0
PURCHASE_TIMEOUT_S = 120.0
LENGTH_PREFIX_BYTES = 4

# result status digit (provisional).                                  # SPEC:
_RESULT_APPROVED = "0"
_RESULT_DECLINED = "1"
_RESULT_CANCELLED = "4"

# command codes.                                                      # SPEC:
_CMD_STATUS = "0"
_CMD_PURCHASE = "1"
_CMD_REFUND = "3"
_CMD_ABORT = "9"
_CMD_MERCHANTS = "80"
_CMD_INFO = "90"
_CMD_LAST = "95"


# ---------------------------------------------------------------------------
# Framing — 4-byte big-endian length prefix + JSON
# ---------------------------------------------------------------------------


def encode_frame(message: dict) -> bytes:
    body = json_bytes(message)
    return len(body).to_bytes(LENGTH_PREFIX_BYTES, "big") + body


async def read_frame(reader: asyncio.StreamReader, timeout: float) -> bytes:
    header = await asyncio.wait_for(
        reader.readexactly(LENGTH_PREFIX_BYTES), timeout=timeout,
    )
    length = int.from_bytes(header, "big")
    if length <= 0 or length > MAX_FRAME_SIZE:
        raise BridgeFrameError(f"implausible BPOS frame length: {length}")
    return await asyncio.wait_for(reader.readexactly(length), timeout=timeout)


def _amount_str(kopecks: int) -> str:
    grn, kop = divmod(int(kopecks), 100)
    return f"{grn}.{kop:02d}"


class BposTerminalAdapter(TerminalAdapter):
    """BPOS1 / BPOS Light over a length-prefixed JSON TCP bridge."""

    @classmethod
    async def probe(
        cls, host: str, port: int = DEFAULT_TCP_PORT,
    ) -> Optional[TerminalDescriptor]:
        try:
            status = await exchange(
                host, port, {"cmd": _CMD_STATUS},
                encode=encode_frame, read_frame=read_frame, timeout=2.5,
            )
        except (TerminalUnavailable, BridgeFrameError):
            return None
        if status.get("cmd") != _CMD_STATUS or "result" not in status:
            return None

        model = serial = None
        try:
            info = await exchange(
                host, port, {"cmd": _CMD_INFO},
                encode=encode_frame, read_frame=read_frame, timeout=5.0,
            )
            model = info.get("model")
            serial = info.get("sn") or info.get("serial")
        except (TerminalUnavailable, BridgeFrameError):
            pass

        terminal_id = make_terminal_id(
            TerminalTransport.network, host, str(port), serial or "",
        )
        label = f"{model or 'BPOS terminal'} @ {host}"
        if serial:
            label = f"{label} (s/n {serial})"
        return TerminalDescriptor(
            id=terminal_id,
            transport=TerminalTransport.network,
            label=label,
            kind=TerminalKind.generic_bpos,
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

    async def _send(self, message: dict, *, timeout: float = REQUEST_TIMEOUT_S) -> dict:
        host, port = self._addr()
        return await exchange(
            host, port, message,
            encode=encode_frame, read_frame=read_frame, timeout=timeout,
        )

    async def ping(self) -> bool:
        try:
            response = await self._send({"cmd": _CMD_STATUS}, timeout=3.0)
        except TerminalUnavailable:
            return False
        return "result" in response

    async def get_info(self) -> dict:
        response = await self._send({"cmd": _CMD_INFO}, timeout=5.0)
        _raise_if_error(response)
        return response

    async def list_merchants(self) -> list[MerchantInfo]:
        response = await self._send({"cmd": _CMD_MERCHANTS}, timeout=5.0)
        _raise_if_error(response)
        out: list[MerchantInfo] = []
        for m in response.get("merchants") or []:
            if not isinstance(m, dict):
                continue
            out.append(MerchantInfo(
                merchant_id=str(m.get("merchantId") or m.get("mid") or ""),
                terminal_id=(str(m["terminalId"]) if m.get("terminalId") else None),
                merchant_name=m.get("name"),
            ))
        return out

    async def charge(self, request: ChargeRequest) -> AcquirerResult:
        merchant_id = (
            request.merchant_id or self.registration.default_merchant_id or ""
        )
        params: dict = {
            "cmd": _CMD_PURCHASE,
            "amount": _amount_str(request.amount_kopecks),   # SPEC: decimal string
            "currency": request.currency,
            "merchantId": str(merchant_id),
        }
        if request.discounted_amount_kopecks is not None:
            params["discount"] = _amount_str(request.discounted_amount_kopecks)
        for k, v in request.extras.items():
            params.setdefault(k, v)
        response = await self._send(params, timeout=PURCHASE_TIMEOUT_S)
        return _result_from_purchase(response, transaction_uid=request.transaction_uid)

    async def cancel(self) -> None:
        try:
            await self._send({"cmd": _CMD_ABORT}, timeout=3.0)
        except TerminalUnavailable:
            logger.debug("[%s] abort failed (likely idle)", self.descriptor.id)

    async def get_last_result(
        self, transaction_uid: Optional[str] = None,
    ) -> AcquirerResult:
        response = await self._send({"cmd": _CMD_LAST}, timeout=5.0)
        return _result_from_purchase(response, transaction_uid=transaction_uid)


# ---------------------------------------------------------------------------
# Response mapping
# ---------------------------------------------------------------------------


def _result_from_purchase(
    response: dict, *, transaction_uid: Optional[str] = None,
) -> AcquirerResult:
    result = str(response.get("result") or "")
    if result == _RESULT_CANCELLED:
        status = "cancelled"
    elif result == _RESULT_APPROVED:
        status = "ok"
    else:
        status = "declined"
    return AcquirerResult(
        status=status,
        transaction_uid=transaction_uid,
        rrn=response.get("rrn"),
        auth_code=response.get("authCode") or response.get("auth"),
        cardmask=response.get("pan"),
        paysys=response.get("cardName") or response.get("scheme"),
        bank_name=response.get("bankName"),
        terminal_id=response.get("terminalId"),
        pos_entry_mode=response.get("entryMode"),
        invoice_num=response.get("invoice") or response.get("invoiceNumber"),
        response_code=str(response.get("responseCode") or "") or None,
        raw_transaction_result=result or None,
        error_code=(result if status == "declined" and result else None),
        error_message=response.get("message") or response.get("errorText"),
        terminal_receipt=response.get("receipt"),
        vendor_data=response,
    )


def _raise_if_error(response: dict, *, default_code: str = "error") -> None:
    result = str(response.get("result") or "")
    if result and result != _RESULT_APPROVED:
        raise TerminalUnavailable(
            response.get("message") or f"BPOS error result={result}",
            code=result or default_code,
        )


__all__ = [
    "BposTerminalAdapter",
    "encode_frame",
    "read_frame",
    "DEFAULT_TCP_PORT",
]
