"""Printec PosAPI terminal emulator (Raiffeisen / PUMB) — device side.

Byte-consistent with ``src/services/terminals/posapi.py``. Newline-delimited
JSON. Single-step PURCHASE blocks on the console decision.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Optional

from .base_terminal import BankEmulator

NEWLINE = b"\n"


class PosApiTerminalEmulator(BankEmulator):
    protocol = "posapi"
    default_port = 8080

    async def read_request(self, reader: asyncio.StreamReader) -> Optional[dict]:
        line = await reader.readuntil(NEWLINE)
        body = line[:-1]
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def encode_response(self, response: dict) -> bytes:
        return json.dumps(response, ensure_ascii=False).encode("utf-8") + NEWLINE

    def handle(self, request: dict) -> dict:
        fn = request.get("function")
        if fn == "ECHO":
            return {"function": "ECHO", "responseCode": "00"}
        if fn == "GET_INFO":
            return {"function": "GET_INFO", "responseCode": "00",
                    "model": self.model, "serial": self.serial}
        if fn == "GET_MERCHANTS":
            return {"function": "GET_MERCHANTS", "responseCode": "00", "merchants": [{
                "merchantId": self.merchant_id, "terminalId": self.terminal_id,
                "name": self.merchant_name,
            }]}
        if fn == "ABORT":
            self.interrupt_current()
            return {"function": "ABORT", "responseCode": "00"}
        if fn == "PURCHASE":
            return self._purchase(request)
        if fn == "GET_LAST":
            return self._approved(request.get("reference"))
        return {"function": fn, "responseCode": "00"}

    def _purchase(self, request: dict) -> dict:
        amount = int(request.get("amount") or 0)
        decision = self._await_decision(amount, str(request.get("currency") or "980"))
        ref = request.get("reference")
        if decision == "c":
            return {"function": "PURCHASE", "responseCode": "UC", "approved": False,
                    "responseText": "Скасовано на терміналі", "reference": ref}
        if decision == "d":
            return {"function": "PURCHASE", "responseCode": "05", "approved": False,
                    "responseText": "Do not honour", "reference": ref}
        return self._approved(ref)

    def _approved(self, ref) -> dict:
        return {"function": "PURCHASE", "responseCode": "00", "approved": True,
                "rrn": f"{random.randint(0, 999999999999):012d}",
                "authCode": f"{random.randint(0, 999999):06d}",
                "pan": "**1234", "cardScheme": "Visa", "bankName": "EMULATOR BANK",
                "terminalId": self.terminal_id,
                "invoiceNumber": f"{random.randint(0, 999999):06d}",
                "reference": ref}
