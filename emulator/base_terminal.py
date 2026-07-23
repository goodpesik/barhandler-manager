"""Shared plumbing for the multi-bank POS-terminal emulator.

Every bank emulator is a `BankEmulator` subclass that provides three things:

    read_request(reader)  -> dict | None   # decode one wire frame
    encode_response(dict)  -> bytes         # encode one wire frame
    handle(dict)           -> dict          # device-side state machine

`serve_forever()` is protocol-agnostic: one framed request/response per TCP
connection (which is how every adapter — SSI, PB and the three bridge
protocols — talks: connect → one frame → one frame → close). `handle()` runs
in a thread-pool executor so a single-step Purchase handler may *block* on the
console operator's approve/decline/cancel decision without stalling the event
loop.

The console handoff is the shared `decisions` queue: `_await_decision()` pushes
a `Pending` and blocks until the console thread sets the outcome — used by the
single-step protocols (Privat / PosAPI / BPOS / Oschad). SSI/Mono is two-step
(ack then poll) and drives the same queue from its own state machine.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Pending:
    """One in-flight Purchase handed from the server thread to the console."""

    amount_kopecks: int
    currency: str
    event: threading.Event = field(default_factory=threading.Event)
    decision: str = "a"  # 'a' approve | 'd' decline | 'c' cancel


class BankEmulator(ABC):
    """Device side of one bank's ECR protocol."""

    #: short protocol id, e.g. "ssi" / "posapi"
    protocol: str = "generic"
    #: default listen port for this protocol
    default_port: int = 0

    def __init__(
        self,
        *,
        decisions: "queue.Queue[Pending]",
        merchant_id: str = "00000012345",
        merchant_name: str = "EMULATOR MERCHANT",
        terminal_id: str = "EMU00001",
        model: str = "BARHANDLER EMULATOR",
        serial: str = "EMU-0001",
        on_traffic: Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        self.decisions = decisions
        self.merchant_id = merchant_id
        self.merchant_name = merchant_name
        self.terminal_id = terminal_id
        self.model = model
        self.serial = serial
        self.on_traffic = on_traffic or (lambda direction, msg: None)
        self.current: Optional[Pending] = None

    # -- console handoff ---------------------------------------------------

    def _await_decision(self, amount_kopecks: int, currency: str) -> str:
        """Block (in the executor thread) until the console operator picks an
        outcome for this Purchase. Returns 'a' | 'd' | 'c'."""
        pending = Pending(amount_kopecks=amount_kopecks, currency=currency)
        self.current = pending
        self.decisions.put(pending)
        pending.event.wait()
        return pending.decision

    def interrupt_current(self) -> None:
        """Cancel an in-flight transaction (adapter `cancel()` / abort)."""
        if self.current and not self.current.event.is_set():
            self.current.decision = "c"
            self.current.event.set()

    # -- protocol surface (implemented per bank) ---------------------------

    @abstractmethod
    async def read_request(self, reader: asyncio.StreamReader) -> Optional[dict]:
        """Read exactly one wire frame; return the decoded dict, or None to
        drop the connection quietly (probe / junk / disconnect)."""

    @abstractmethod
    def encode_response(self, response: dict) -> bytes:
        ...

    @abstractmethod
    def handle(self, request: dict) -> dict:
        """Device-side dispatch. May block (runs in an executor)."""


# ---------------------------------------------------------------------------
# Generic framed TCP server
# ---------------------------------------------------------------------------


async def _serve_client(reader, writer, emulator: BankEmulator) -> None:
    try:
        request = await emulator.read_request(reader)
        if request is None:
            return
        emulator.on_traffic("recv", request)
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, emulator.handle, request)
        emulator.on_traffic("send", response)
        writer.write(emulator.encode_response(response))
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError, ValueError):
        # probe / disconnect / malformed frame — ignore quietly
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


class EmulatorServer:
    """Owns the asyncio loop + server socket on a daemon thread so the main
    thread keeps the interactive console. `stop()` tears it down cleanly so
    the operator can switch to another bank on the same port."""

    def __init__(self, host: str, port: int, emulator: BankEmulator) -> None:
        self.host = host
        self.port = port
        self.emulator = emulator
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self) -> None:
        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._boot())
            self._ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run, name=f"emu-{self.emulator.protocol}", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    async def _boot(self) -> None:
        self._server = await asyncio.start_server(
            lambda r, w: _serve_client(r, w, self.emulator), self.host, self.port,
        )
        # Reflect the actually-bound port (matters when port=0 for tests).
        self.port = self._server.sockets[0].getsockname()[1]

    def stop(self) -> None:
        if self._loop is None:
            return
        loop = self._loop

        async def _shutdown() -> None:
            if self._server is not None:
                self._server.close()
                try:
                    await self._server.wait_closed()
                except Exception:  # noqa: BLE001
                    pass

        try:
            fut = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
            fut.result(timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
