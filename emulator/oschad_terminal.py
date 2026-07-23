"""Oschadbank ECR terminal emulator — device side.

Byte-consistent with ``src/services/terminals/oschad.py``. STX(0x02) + JSON +
ETX(0x03) framing; `rc` "000" = approved. Single-step `sale` blocks on the
console decision.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Optional

from .base_terminal import BankEmulator

STX_BYTE = b"\x02"
ETX_BYTE = b"\x03"


class OschadTerminalEmulator(BankEmulator):
    protocol = "oschad"
    default_port = 7777

    async def read_request(self, reader: asyncio.StreamReader) -> Optional[dict]:
        raw = await reader.readuntil(ETX_BYTE)
        body = raw[:-1]
        if body[:1] == STX_BYTE:
            body = body[1:]
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def encode_response(self, response: dict) -> bytes:
        body = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return STX_BYTE + body + ETX_BYTE

    def handle(self, request: dict) -> dict:
        op = request.get("op")
        if op == "ping":
            return {"op": "ping", "rc": "000"}
        if op == "info":
            return {"op": "info", "rc": "000", "model": self.model, "sn": self.serial}
        if op == "merchants":
            return {"op": "merchants", "rc": "000", "merchants": [{
                "mid": self.merchant_id, "name": self.merchant_name,
            }]}
        if op == "abort":
            self.interrupt_current()
            return {"op": "abort", "rc": "000"}
        if op == "sale":
            return self._sale(request)
        if op == "last":
            return self._approved("last")
        return {"op": op, "rc": "000"}

    def _sale(self, request: dict) -> dict:
        amount = int(request.get("sum") or 0)
        decision = self._await_decision(amount, str(request.get("ccy") or "980"))
        if decision == "c":
            return {"op": "sale", "rc": "100", "msg": "Скасовано"}
        if decision == "d":
            return {"op": "sale", "rc": "051", "msg": "Відхилено (емулятор)"}
        return self._approved("sale")

    def _approved(self, op: str) -> dict:
        return {"op": op, "rc": "000",
                "rrn": f"{random.randint(0, 999999999999):012d}",
                "auth": f"{random.randint(0, 999999):06d}",
                "pan": "**9999", "scheme": "Visa", "bankName": "Oschadbank",
                "tid": self.terminal_id,
                "invoice": f"{random.randint(0, 999999):06d}"}
