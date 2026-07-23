"""BPOS1 / BPOS Light terminal emulator (Pivdenny / Sense) — device side.

Byte-consistent with ``src/services/terminals/bpos.py``. 4-byte big-endian
length prefix + JSON. Numeric command codes; `result` "0" = approved.
Single-step purchase (cmd "1") blocks on the console decision.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Optional

from .base_terminal import BankEmulator

LEN = 4


class BposTerminalEmulator(BankEmulator):
    protocol = "bpos"
    default_port = 8888

    async def read_request(self, reader: asyncio.StreamReader) -> Optional[dict]:
        header = await reader.readexactly(LEN)
        length = int.from_bytes(header, "big")
        if length <= 0:
            return None
        body = await reader.readexactly(length)
        return json.loads(body.decode("utf-8"))

    def encode_response(self, response: dict) -> bytes:
        body = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return len(body).to_bytes(LEN, "big") + body

    def handle(self, request: dict) -> dict:
        cmd = request.get("cmd")
        if cmd == "0":
            return {"cmd": "0", "result": "0"}
        if cmd == "90":
            return {"cmd": "90", "result": "0", "model": self.model, "sn": self.serial}
        if cmd == "80":
            return {"cmd": "80", "result": "0", "merchants": [{
                "merchantId": self.merchant_id, "name": self.merchant_name,
            }]}
        if cmd == "9":
            self.interrupt_current()
            return {"cmd": "9", "result": "0"}
        if cmd == "1":
            return self._purchase(request)
        if cmd == "95":
            return self._approved("95")
        return {"cmd": cmd, "result": "0"}

    def _purchase(self, request: dict) -> dict:
        amount = _amount_to_kopecks(request.get("amount"))
        decision = self._await_decision(amount, str(request.get("currency") or "980"))
        if decision == "c":
            return {"cmd": "1", "result": "4", "message": "Скасовано"}
        if decision == "d":
            return {"cmd": "1", "result": "1", "message": "Відхилено (емулятор)"}
        return self._approved("1")

    def _approved(self, cmd: str) -> dict:
        return {"cmd": cmd, "result": "0",
                "rrn": f"{random.randint(0, 999999999999):012d}",
                "authCode": f"{random.randint(0, 999999):06d}",
                "pan": "**5678", "cardName": "MasterCard", "bankName": "EMULATOR BANK",
                "terminalId": self.terminal_id,
                "invoice": f"{random.randint(0, 999999):06d}"}


def _amount_to_kopecks(raw) -> int:
    if raw is None:
        return 0
    try:
        return int(round(float(str(raw).replace(",", ".")) * 100))
    except ValueError:
        return 0
