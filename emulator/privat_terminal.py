"""PrivatBank ECR JSON terminal emulator — device side.

Byte-compatible with ``src/services/terminals/privatbank.py``.
Framing: JSON UTF-8 + a trailing 0x00; the adapter's first PingDevice also
carries a leading 0x00, which we tolerate. Purchase is single-step: the
adapter sends one Purchase and blocks, so we block on the console decision
and answer in the same connection.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Optional

from .base_terminal import BankEmulator

DELIMITER = 0x00
DELIMITER_BYTE = bytes([DELIMITER])


def encode_frame(message: dict) -> bytes:
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return data + DELIMITER_BYTE


class PrivatTerminalEmulator(BankEmulator):
    protocol = "privat"
    default_port = 2000

    async def read_request(self, reader: asyncio.StreamReader) -> Optional[dict]:
        raw = await reader.readuntil(DELIMITER_BYTE)
        body = raw[:-1]
        if not body:
            # That was the handshake's leading 0x00 (first PingDevice on a
            # fresh connection is `0x00 + json + 0x00`) — read the real frame.
            raw = await reader.readuntil(DELIMITER_BYTE)
            body = raw[:-1]
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def encode_response(self, response: dict) -> bytes:
        return encode_frame(response)

    def handle(self, request: dict) -> dict:
        method = request.get("method")
        if method == "PingDevice":
            return {"method": "PingDevice", "error": False}
        if method == "ServiceMessage":
            return self._service(request.get("params") or {})
        if method == "Purchase":
            return self._purchase(request.get("params") or {})
        if method in ("Refund", "GetReceiptInfo"):
            return self._purchase(request.get("params") or {})
        return {"error": False}

    def _service(self, params: dict) -> dict:
        msg_type = params.get("msgType")
        if msg_type == "identify":
            return {"error": False, "params": {
                "vendor": "Ingenico", "model": "Desk 5000", "serialNumber": self.serial,
            }}
        if msg_type == "getMerchantList":
            # index-based dict, per PB spec §6.4
            return {"error": False, "params": {"0": self.merchant_name}}
        if msg_type == "interrupt":
            self.interrupt_current()
            return {"error": False, "params": {"interruptTransmitted": True}}
        if msg_type == "getLastResult":
            ok = bool(self.current and self.current.decision == "a")
            return {"error": False, "params": {"LastResult": "0" if ok else "2"}}
        return {"error": False, "params": {}}

    def _purchase(self, params: dict) -> dict:
        amount_kopecks = _amount_to_kopecks(params.get("amount"))
        decision = self._await_decision(amount_kopecks, "980")
        if decision == "c":
            return {"method": "Purchase", "error": True,
                    "errorDescription": "Скасовано користувачем",
                    "params": {"responseCode": "1001", "trnStatus": "4"}}
        if decision == "d":
            return {"method": "Purchase", "error": True,
                    "errorDescription": "Відхилено (емулятор)",
                    "params": {"responseCode": "0500", "trnStatus": "2"}}
        return {"method": "Purchase", "error": False, "params": {
            "trnStatus": "1",
            "responseCode": "0000",
            "rrn": f"{random.randint(0, 999999999999):012d}",
            "approvalCode": f"{random.randint(0, 999999):06d}",
            "pan": "**1234",
            "paymentSystem": "Visa",
            "bankAcquirer": "EMULATOR BANK",
            "terminalId": self.terminal_id,
            "posEntryMode": "07",
            "invoiceNumber": f"{random.randint(0, 999999):06d}",
        }}


def _amount_to_kopecks(raw) -> int:
    if raw is None:
        return 0
    text = str(raw).replace(",", ".").strip()
    try:
        return int(round(float(text) * 100))
    except ValueError:
        return 0
