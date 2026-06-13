"""SSI ECR JSON terminal emulator — the device side of the wire.

Byte-compatible with barhandler-manager's ``src/services/terminals/ssi.py``
but dependency-free (it re-implements the ~20 lines of framing rather than
importing the manager's app package, so the emulator runs standalone).

Frame layout (SSI doc §1.3):
    STX(02 66 01) + LEN(2B big-endian, DATA only) + DATA(UTF-8 JSON) + LRC(1B)
    LRC = XOR of every DATA byte.

The manager opens a fresh TCP connection per request (connect → one frame →
one response → close), so terminal state lives on the emulator object, not
per-connection. We model Mono's two-step Purchase:

    Purchase step:1  → ack, status S08 ("waiting for second step")
    GetLastResult    → transactionResult "FIRST_STEP_COMPLETED"
    Purchase step:2  → ack, status stays busy (S02) until the operator picks
                       an outcome in the console, then S00
    GetLastResult    → final APPROVED / DECLINED / CANCELLED
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import random
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

log = logging.getLogger("emulator.ssi")

STX_PREFIX = bytes([0x02, 0x66, 0x01])

# SSI statuses: S00 idle/done, S02 busy, S08 waiting for the second step.
S_IDLE = "S00"
S_BUSY = "S02"
S_SECOND_STEP = "S08"


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def calc_lrc(payload: bytes) -> int:
    lrc = 0
    for b in payload:
        lrc ^= b
    return lrc


def encode_frame(message: dict) -> bytes:
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    return STX_PREFIX + len(data).to_bytes(2, "big") + data + bytes([calc_lrc(data)])


# ---------------------------------------------------------------------------
# Transaction handoff between the asyncio server thread and the console thread
# ---------------------------------------------------------------------------


@dataclass
class Pending:
    """One in-flight Purchase. The server thread creates it and pushes it to
    the console thread, which sets ``decision`` ('a'/'d'/'c') and the event."""

    amount_kopecks: int
    currency: str
    event: threading.Event = field(default_factory=threading.Event)
    decision: str = "a"


# ---------------------------------------------------------------------------
# Emulator state machine
# ---------------------------------------------------------------------------


class SSITerminalEmulator:
    def __init__(
        self,
        *,
        decisions: "queue.Queue[Pending]",
        merchant_id: str = "00000012345",
        merchant_name: str = "EMULATOR MERCHANT",
        terminal_id: str = "EMU00001",
        model: str = "BARHANDLER EMULATOR",
        serial: str = "EMU-0001",
        package: str = "com.monobank.acquiring",
        on_traffic: Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        self.decisions = decisions
        self.merchant_id = merchant_id
        self.merchant_name = merchant_name
        self.terminal_id = terminal_id
        self.model = model
        self.serial = serial
        self.package = package
        self.on_traffic = on_traffic or (lambda direction, msg: None)

        self.phase = 0  # 0 idle, 1 after step1, 2 after step2
        self.current: Optional[Pending] = None

    # -- dispatch ----------------------------------------------------------

    def handle(self, message: dict) -> dict:
        method = message.get("method")
        self.on_traffic("recv", message)
        response = self._dispatch(method, message)
        self.on_traffic("send", response)
        return response

    def _dispatch(self, method: Optional[str], message: dict) -> dict:
        if method == "PingDevice":
            return {"method": "PingDevice", "error": False}
        if method == "GetTerminalInfo":
            return {
                "error": False,
                "params": {
                    "terminalModel": self.model,
                    "terminalSerialNumber": self.serial,
                    "currentApp": {"packageName": self.package},
                },
            }
        if method == "GetMerchantListDetailed":
            return {
                "error": False,
                "params": {
                    "merchantList": [
                        {
                            "merchantId": self.merchant_id,
                            "terminalId": self.terminal_id,
                            "merchantName": self.merchant_name,
                        }
                    ]
                },
            }
        if method == "Purchase":
            return self._purchase(message)
        if method == "GetStatus":
            return {"error": False, "status": self._status()}
        if method in ("GetLastResult", "GetResultByUid"):
            return self._result()
        if method == "GetLastReceipt":
            return {"error": False, "params": {"receipt": self._receipt()}}
        if method == "Interrupt":
            if self.current and not self.current.event.is_set():
                self.current.decision = "c"
                self.current.event.set()
            self.phase = 2
            return {"error": False}
        # Be permissive about anything else the manager probes with.
        return {"error": False}

    # -- purchase flow -----------------------------------------------------

    def _purchase(self, message: dict) -> dict:
        step = str(message.get("step") or "1")
        params = message.get("params") or {}
        if step == "1":
            self.current = Pending(
                amount_kopecks=int(params.get("transAmount") or 0),
                currency=str(params.get("transCurrency") or "980"),
            )
            self.phase = 1
            # Hand the transaction to the console thread to prompt the operator.
            self.decisions.put(self.current)
            return {"error": False}
        # step 2 — authorisation pass; stays busy until the operator decides.
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
            return {
                "error": False,
                "params": {
                    "transactionResult": "FIRST_STEP_COMPLETED",
                    "transactionUid": self._uid(),
                },
            }
        decision = self.current.decision if self.current else "a"
        if decision == "a":
            now = datetime.now()
            return {
                "error": False,
                "params": {
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
                },
            }
        if decision == "c":
            return {"error": False, "params": {"transactionResult": "CANCELLED"}}
        # decline
        return {
            "error": False,
            "params": {
                "transactionResult": "DECLINED",
                "responseCode": "05",
                "errorDescription": "Картку відхилено (емулятор)",
            },
        }

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


# ---------------------------------------------------------------------------
# asyncio TCP server (one framed request/response per connection)
# ---------------------------------------------------------------------------


async def _serve_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    emulator: SSITerminalEmulator,
) -> None:
    try:
        header = await reader.readexactly(5)
        if header[:3] != STX_PREFIX:
            return
        data_len = int.from_bytes(header[3:5], "big")
        rest = await reader.readexactly(data_len + 1)  # DATA + LRC
        message = json.loads(rest[:data_len].decode("utf-8"))
        response = emulator.handle(message)
        writer.write(encode_frame(response))
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError, json.JSONDecodeError):
        # Manager probing / disconnecting / a non-SSI client — ignore quietly.
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def run_server(host: str, port: int, emulator: SSITerminalEmulator) -> None:
    server = await asyncio.start_server(
        lambda r, w: _serve_client(r, w, emulator), host, port
    )
    async with server:
        await server.serve_forever()


def start_server_thread(
    host: str, port: int, emulator: SSITerminalEmulator
) -> threading.Thread:
    """Run the asyncio server on its own loop in a daemon thread so the main
    thread is free to own the interactive console."""

    def _run() -> None:
        asyncio.run(run_server(host, port, emulator))

    thread = threading.Thread(target=_run, name="ssi-emulator-server", daemon=True)
    thread.start()
    return thread
