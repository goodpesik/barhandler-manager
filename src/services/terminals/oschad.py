"""Oschadbank ECR adapter.

Oschadbank integrates its POS terminals through the bank's own ECR driver
(shipped as ECR + USB drivers, connectable over network / USB / COM). The
classic ECR wire style is a control-character-framed message — we model the
network variant as **STX … ETX** wrapped JSON reached through a local bank
ECR bridge next to the manager.

The official Oschad ECR spec is not public (see the terminal-protocol
research note); the `op`/`rc` vocabulary below is our best-effort model,
byte-consistent with `emulator/oschad_terminal.py`. Real doc values are
marked `# SPEC:`.

Wire model (provisional):
    Framing : STX(0x02) + JSON body + ETX(0x03).                      # SPEC:
    Port    : 7777 (bank ECR bridge default).                         # SPEC:
    Amount  : integer minor units (kopecks) in `sum`.                 # SPEC:
    Purchase: single request/response (bridge blocks until done).
Operations: ping, info, merchants, sale, refund, abort, last.        # SPEC:
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

DEFAULT_TCP_PORT = 7777          # SPEC: Oschad ECR bridge default
REQUEST_TIMEOUT_S = 15.0
PURCHASE_TIMEOUT_S = 120.0

STX = 0x02
ETX = 0x03
STX_BYTE = bytes([STX])
ETX_BYTE = bytes([ETX])

# response-code vocabulary (provisional).                             # SPEC:
_RC_APPROVED = "000"
_RC_CANCELLED = "100"
# Any other rc → declined.


# ---------------------------------------------------------------------------
# Framing — STX + JSON + ETX
# ---------------------------------------------------------------------------


def encode_frame(message: dict) -> bytes:
    return STX_BYTE + json_bytes(message) + ETX_BYTE


async def read_frame(reader: asyncio.StreamReader, timeout: float) -> bytes:
    """Read one STX…ETX frame, return the JSON body between the markers.
    Tolerates a missing leading STX (some bridges omit it on replies)."""
    raw = await asyncio.wait_for(reader.readuntil(ETX_BYTE), timeout=timeout)
    body = raw[:-1]                       # drop ETX
    if body[:1] == STX_BYTE:
        body = body[1:]                   # drop leading STX if present
    if len(body) > MAX_FRAME_SIZE:
        raise BridgeFrameError(f"Oschad frame too large: {len(body)} bytes")
    return body


class OschadTerminalAdapter(TerminalAdapter):
    """Oschadbank ECR over an STX/ETX JSON TCP bridge."""

    @classmethod
    async def probe(
        cls, host: str, port: int = DEFAULT_TCP_PORT,
    ) -> Optional[TerminalDescriptor]:
        try:
            pong = await exchange(
                host, port, {"op": "ping"},
                encode=encode_frame, read_frame=read_frame, timeout=2.5,
            )
        except (TerminalUnavailable, BridgeFrameError):
            return None
        if pong.get("op") != "ping" or pong.get("rc") != _RC_APPROVED:
            return None

        model = serial = None
        try:
            info = await exchange(
                host, port, {"op": "info"},
                encode=encode_frame, read_frame=read_frame, timeout=5.0,
            )
            model = info.get("model")
            serial = info.get("sn") or info.get("serial")
        except (TerminalUnavailable, BridgeFrameError):
            pass

        terminal_id = make_terminal_id(
            TerminalTransport.network, host, str(port), serial or "",
        )
        label = f"{model or 'Oschad POS'} @ {host}"
        if serial:
            label = f"{label} (s/n {serial})"
        return TerminalDescriptor(
            id=terminal_id,
            transport=TerminalTransport.network,
            label=label,
            kind=TerminalKind.oschad_pos,
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
            response = await self._send({"op": "ping"}, timeout=3.0)
        except TerminalUnavailable:
            return False
        return response.get("rc") == _RC_APPROVED

    async def get_info(self) -> dict:
        response = await self._send({"op": "info"}, timeout=5.0)
        _raise_if_error(response)
        return response

    async def list_merchants(self) -> list[MerchantInfo]:
        response = await self._send({"op": "merchants"}, timeout=5.0)
        _raise_if_error(response)
        out: list[MerchantInfo] = []
        for m in response.get("merchants") or []:
            if not isinstance(m, dict):
                continue
            out.append(MerchantInfo(
                merchant_id=str(m.get("mid") or m.get("merchantId") or ""),
                terminal_id=(str(m["tid"]) if m.get("tid") else None),
                merchant_name=m.get("name"),
            ))
        return out

    async def charge(self, request: ChargeRequest) -> AcquirerResult:
        merchant_id = (
            request.merchant_id or self.registration.default_merchant_id or ""
        )
        params: dict = {
            "op": "sale",
            "sum": int(request.amount_kopecks),              # SPEC: minor units
            "ccy": request.currency,
            "mid": str(merchant_id),
        }
        if request.discounted_amount_kopecks is not None:
            params["discount"] = int(request.discounted_amount_kopecks)
        for k, v in request.extras.items():
            params.setdefault(k, v)
        response = await self._send(params, timeout=PURCHASE_TIMEOUT_S)
        return _result_from_purchase(response, transaction_uid=request.transaction_uid)

    async def cancel(self) -> None:
        try:
            await self._send({"op": "abort"}, timeout=3.0)
        except TerminalUnavailable:
            logger.debug("[%s] abort failed (likely idle)", self.descriptor.id)

    async def get_last_result(
        self, transaction_uid: Optional[str] = None,
    ) -> AcquirerResult:
        response = await self._send({"op": "last"}, timeout=5.0)
        return _result_from_purchase(response, transaction_uid=transaction_uid)


# ---------------------------------------------------------------------------
# Response mapping
# ---------------------------------------------------------------------------


def _result_from_purchase(
    response: dict, *, transaction_uid: Optional[str] = None,
) -> AcquirerResult:
    rc = str(response.get("rc") or "")
    if rc == _RC_CANCELLED:
        status = "cancelled"
    elif rc == _RC_APPROVED:
        status = "ok"
    else:
        status = "declined"
    return AcquirerResult(
        status=status,
        transaction_uid=transaction_uid,
        rrn=response.get("rrn"),
        auth_code=response.get("auth") or response.get("authCode"),
        cardmask=response.get("pan"),
        paysys=response.get("scheme") or response.get("cardName"),
        bank_name=response.get("bankName") or "Oschadbank",
        terminal_id=response.get("tid") or response.get("terminalId"),
        pos_entry_mode=response.get("entryMode"),
        invoice_num=response.get("invoice"),
        response_code=rc or None,
        raw_transaction_result=rc or None,
        error_code=(rc if status == "declined" and rc else None),
        error_message=response.get("msg") or response.get("message"),
        terminal_receipt=response.get("receipt"),
        vendor_data=response,
    )


def _raise_if_error(response: dict, *, default_code: str = "error") -> None:
    rc = str(response.get("rc") or "")
    if rc and rc != _RC_APPROVED:
        raise TerminalUnavailable(
            response.get("msg") or f"Oschad error rc={rc}",
            code=rc or default_code,
        )


__all__ = [
    "OschadTerminalAdapter",
    "encode_frame",
    "read_frame",
    "DEFAULT_TCP_PORT",
]
