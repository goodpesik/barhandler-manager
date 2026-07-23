"""SSI ECR JSON terminal emulator (Monobank / generic-SSI) — device side.

Byte-compatible with ``src/services/terminals/ssi.py``. Two-step Mono Purchase:

    Purchase step:1  → ack, GetStatus returns S08 ("waiting for second step")
    GetLastResult    → transactionResult "FIRST_STEP_COMPLETED"
    Purchase step:2  → ack, GetStatus busy (S02) until the console operator
                       decides, then S00 (idle/done)
    GetLastResult    → final APPROVED / DECLINED / CANCELLED

Framing (SSI doc §1.3): STX(02 66 01) + LEN(2B big-endian, DATA only) +
DATA(UTF-8 JSON) + LRC(1B), LRC = XOR of the DATA bytes.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime
from typing import Optional

from .base_terminal import BankEmulator, Pending

STX_PREFIX = bytes([0x02, 0x66, 0x01])

S_IDLE = "S00"
S_BUSY = "S02"
S_SECOND_STEP = "S08"


def calc_lrc(payload: bytes) -> int:
    lrc = 0
    for b in payload:
        lrc ^= b
    return lrc


def encode_frame(message: dict) -> bytes:
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    return STX_PREFIX + len(data).to_bytes(2, "big") + data + bytes([calc_lrc(data)])


class SSITerminalEmulator(BankEmulator):
    protocol = "ssi"
    default_port = 3000

    def __init__(self, *, package: str = "com.monobank.acquiring", **kwargs) -> None:
        super().__init__(**kwargs)
        self.package = package
        self.phase = 0  # 0 idle, 1 after step1, 2 after step2

    async def read_request(self, reader: asyncio.StreamReader) -> Optional[dict]:
        header = await reader.readexactly(5)
        if header[:3] != STX_PREFIX:
            return None
        data_len = int.from_bytes(header[3:5], "big")
        rest = await reader.readexactly(data_len + 1)  # DATA + LRC
        return json.loads(rest[:data_len].decode("utf-8"))

    def encode_response(self, response: dict) -> bytes:
        return encode_frame(response)

    def handle(self, request: dict) -> dict:
        method = request.get("method")
        if method == "PingDevice":
            return {"method": "PingDevice", "error": False}
        if method == "GetTerminalInfo":
            return {"error": False, "params": {
                "terminalModel": self.model,
                "terminalSerialNumber": self.serial,
                "currentApp": {"packageName": self.package},
            }}
        if method == "GetMerchantListDetailed":
            return {"error": False, "params": {"merchantList": [{
                "merchantId": self.merchant_id,
                "terminalId": self.terminal_id,
                "merchantName": self.merchant_name,
            }]}}
        if method == "Purchase":
            return self._purchase(request)
        if method == "GetStatus":
            return {"error": False, "status": self._status()}
        if method in ("GetLastResult", "GetResultByUid"):
            return self._result()
        if method == "GetLastReceipt":
            return {"error": False, "params": {"receipt": self._receipt()}}
        if method == "Interrupt":
            self.interrupt_current()
            self.phase = 2
            return {"error": False}
        return {"error": False}

    def _purchase(self, message: dict) -> dict:
        step = str(message.get("step") or "1")
        params = message.get("params") or {}
        if step == "1":
            self.current = Pending(
                amount_kopecks=int(params.get("transAmount") or 0),
                currency=str(params.get("transCurrency") or "980"),
            )
            self.phase = 1
            self.decisions.put(self.current)  # console prompts; result polled
            return {"error": False}
        self.phase = 2
        return {"error": False}

    def _status(self) -> str:
        if self.phase == 1:
            return S_SECOND_STEP
        if self.phase == 2:
            decided = bool(self.current and self.current.event.is_set())
            return S_IDLE if decided else S_BUSY
        return S_IDLE

    def _result(self) -> dict:
        if self.phase == 1:
            return {"error": False, "params": {
                "transactionResult": "FIRST_STEP_COMPLETED",
                "transactionUid": self._uid(),
            }}
        decision = self.current.decision if self.current else "a"
        if decision == "a":
            now = datetime.now()
            return {"error": False, "params": {
                "transactionResult": "APPROVED",
                "transactionUid": self._uid(),
                "rrn": f"{random.randint(0, 999999999999):012d}",
                "authCode": f"{random.randint(0, 999999):06d}",
                "pan": "************1234",
                "binName": "Visa",
                "bankName": "EMULATOR BANK",
                "terminalId": self.terminal_id,
                "merchantId": self.merchant_id,
                "responseCode": "00",
                "posEntryMode": "07",
                "invoiceNum": f"{random.randint(0, 999999):06d}",
                "transactionDate": now.strftime("%d%m%Y"),
                "transactionTime": now.strftime("%H%M%S"),
            }}
        if decision == "c":
            return {"error": False, "params": {"transactionResult": "CANCELLED"}}
        return {"error": False, "params": {
            "transactionResult": "DECLINED",
            "responseCode": "05",
            "errorDescription": "Картку відхилено (емулятор)",
        }}

    def _receipt(self) -> str:
        return (
            "      EMULATOR BANK\n"
            f"  {self.merchant_name}\n"
            f"  TID {self.terminal_id}  MID {self.merchant_id}\n"
            "  ОПЛАТА (емулятор)\n"
            "  ============================\n"
            "  СПЛАЧЕНО — тестова транзакція\n"
        )

    def _uid(self) -> str:
        if self.current is None:
            return "emu-0"
        return f"emu-{id(self.current) & 0xFFFFFF:06x}"
