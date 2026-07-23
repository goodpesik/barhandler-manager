"""Verifone Printec PosAPI adapter — Raiffeisen Bank Aval / PUMB.

Raiffeisen and PUMB ship Verifone terminals running Printec's payment
application, integrated through **Printec PosAPI**. In the field that API
is reached not by talking to the terminal directly but through a small
local bridge/service (the Printec "PosApi" host, or an aggregator like
СОТА Агент / Checkbox PayLink) that the operator runs next to the manager.
Over the wire it's newline-delimited JSON on a local TCP port.

We could not obtain the official Printec PosAPI specification (it is
partner/NDA-gated — see the terminal-protocol research note), so the exact
field names and the sync-vs-async purchase flow below are our best-effort
model, kept byte-consistent with the bundled emulator
(`emulator/posapi_terminal.py`). Everywhere a real doc value is needed it is
marked `# SPEC:` — when the specification lands, only those constants and
the field mapping in `_result_from_purchase` change; the transport,
registry wiring and route layer are already correct.

Wire model (provisional):
    Framing : one JSON object per line, terminated by `\n` (LF).
    Port    : 8080 (Printec PosApi local bridge default).             # SPEC:
    Amount  : integer minor units (kopecks).                          # SPEC:
    Purchase: single request/response — the bridge blocks until the
              cardholder finishes (chip/PIN), like PrivatBank, not the
              two-step ack-then-poll SSI/Mono flow.
Functions : ECHO, GET_INFO, GET_MERCHANTS, PURCHASE, VOID, ABORT, GET_LAST.
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
from src.services.terminals.bridge import BridgeFrameError, exchange, json_bytes

logger = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 8080          # SPEC: Printec PosApi local bridge default
REQUEST_TIMEOUT_S = 15.0
PURCHASE_TIMEOUT_S = 120.0       # cardholder chip+PIN can take a minute+
NEWLINE = b"\n"

# responseCode vocabulary (provisional).                              # SPEC:
_RC_APPROVED = "00"
_RC_CANCELLED = "UC"             # user-cancelled at the PINpad
# Everything else (e.g. "05" do-not-honour) → declined.


# ---------------------------------------------------------------------------
# Framing — newline-delimited JSON
# ---------------------------------------------------------------------------


def encode_frame(message: dict) -> bytes:
    return json_bytes(message) + NEWLINE


async def read_frame(reader: asyncio.StreamReader, timeout: float) -> bytes:
    """Read one LF-terminated JSON line, return the payload without the LF."""
    try:
        line = await asyncio.wait_for(reader.readuntil(NEWLINE), timeout=timeout)
    except asyncio.LimitOverrunError as exc:  # line longer than the stream limit
        raise BridgeFrameError(f"frame exceeds reader limit: {exc}") from exc
    return line[:-1]  # strip the trailing LF


class PosApiTerminalAdapter(TerminalAdapter):
    """Printec PosAPI over a local newline-JSON TCP bridge."""

    @classmethod
    async def probe(
        cls, host: str, port: int = DEFAULT_TCP_PORT,
    ) -> Optional[TerminalDescriptor]:
        """ECHO handshake, then GET_INFO to flesh out the descriptor.
        Returns None on any failure so LAN discovery moves on quietly."""
        try:
            echo = await exchange(
                host, port, {"function": "ECHO"},
                encode=encode_frame, read_frame=read_frame, timeout=2.5,
            )
        except (TerminalUnavailable, BridgeFrameError):
            return None
        if echo.get("function") != "ECHO" or echo.get("responseCode") != _RC_APPROVED:
            return None

        model = serial = None
        try:
            info = await exchange(
                host, port, {"function": "GET_INFO"},
                encode=encode_frame, read_frame=read_frame, timeout=5.0,
            )
            model = info.get("model")
            serial = info.get("serial")
        except (TerminalUnavailable, BridgeFrameError):
            pass  # descriptor still useful without model/serial

        terminal_id = make_terminal_id(
            TerminalTransport.network, host, str(port), serial or "",
        )
        label = f"{model or 'Printec PosAPI'} @ {host}"
        if serial:
            label = f"{label} (s/n {serial})"
        return TerminalDescriptor(
            id=terminal_id,
            transport=TerminalTransport.network,
            label=label,
            kind=TerminalKind.generic_posapi,
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
            response = await self._send({"function": "ECHO"}, timeout=3.0)
        except TerminalUnavailable:
            return False
        return response.get("responseCode") == _RC_APPROVED

    async def get_info(self) -> dict:
        response = await self._send({"function": "GET_INFO"}, timeout=5.0)
        _raise_if_error(response)
        return response

    async def list_merchants(self) -> list[MerchantInfo]:
        response = await self._send({"function": "GET_MERCHANTS"}, timeout=5.0)
        _raise_if_error(response)
        out: list[MerchantInfo] = []
        for m in response.get("merchants") or []:
            if not isinstance(m, dict):
                continue
            out.append(MerchantInfo(
                merchant_id=str(m.get("merchantId") or ""),
                terminal_id=(str(m["terminalId"]) if m.get("terminalId") else None),
                merchant_name=m.get("name"),
            ))
        return out

    async def charge(self, request: ChargeRequest) -> AcquirerResult:
        """Single request/response Purchase — the bridge blocks until the
        cardholder finishes. If the operator hits Cancel, a parallel
        `cancel()` fires ABORT and this response comes back CANCELLED."""
        merchant_id = (
            request.merchant_id or self.registration.default_merchant_id or ""
        )
        params: dict = {
            "function": "PURCHASE",
            "amount": int(request.amount_kopecks),          # SPEC: minor units
            "currency": request.currency,
            "merchantId": str(merchant_id),
        }
        if request.transaction_uid:
            params["reference"] = request.transaction_uid
        if request.discounted_amount_kopecks is not None:
            params["discountAmount"] = int(request.discounted_amount_kopecks)
        for k, v in request.extras.items():
            params.setdefault(k, v)
        response = await self._send(params, timeout=PURCHASE_TIMEOUT_S)
        return _result_from_purchase(response, transaction_uid=request.transaction_uid)

    async def cancel(self) -> None:
        try:
            await self._send({"function": "ABORT"}, timeout=3.0)
        except TerminalUnavailable:
            logger.debug("[%s] ABORT failed (likely already idle)", self.descriptor.id)

    async def get_last_result(
        self, transaction_uid: Optional[str] = None,
    ) -> AcquirerResult:
        req: dict = {"function": "GET_LAST"}
        if transaction_uid:
            req["reference"] = transaction_uid
        response = await self._send(req, timeout=5.0)
        return _result_from_purchase(response, transaction_uid=transaction_uid)


# ---------------------------------------------------------------------------
# Response mapping
# ---------------------------------------------------------------------------


def _result_from_purchase(
    response: dict, *, transaction_uid: Optional[str] = None,
) -> AcquirerResult:
    """Map a PURCHASE/GET_LAST response onto the unified AcquirerResult."""
    rc = str(response.get("responseCode") or "")
    approved = bool(response.get("approved")) and rc == _RC_APPROVED
    if rc == _RC_CANCELLED:
        status = "cancelled"
    elif approved:
        status = "ok"
    else:
        status = "declined"
    return AcquirerResult(
        status=status,
        transaction_uid=transaction_uid or response.get("reference"),
        rrn=response.get("rrn"),
        auth_code=response.get("authCode"),
        cardmask=response.get("pan"),
        paysys=response.get("cardScheme"),
        bank_name=response.get("bankName") or response.get("acquirer"),
        terminal_id=response.get("terminalId"),
        pos_entry_mode=response.get("entryMode"),
        invoice_num=response.get("invoiceNumber") or response.get("transactionId"),
        response_code=rc or None,
        raw_transaction_result=rc or None,
        error_code=(rc if status == "declined" and rc else None),
        error_message=response.get("responseText") or response.get("message"),
        terminal_receipt=response.get("receipt"),
        vendor_data=response,
    )


def _raise_if_error(response: dict, *, default_code: str = "error") -> None:
    """Non-transactional calls (GET_INFO, GET_MERCHANTS): a non-"00"
    responseCode means the bridge rejected the request."""
    rc = str(response.get("responseCode") or "")
    if rc and rc != _RC_APPROVED:
        raise TerminalUnavailable(
            response.get("responseText") or f"PosAPI error {rc}",
            code=rc or default_code,
        )


__all__ = [
    "PosApiTerminalAdapter",
    "encode_frame",
    "read_frame",
    "DEFAULT_TCP_PORT",
]
