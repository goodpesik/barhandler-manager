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
from typing import Callable, Optional, Sequence


#: Що саме підтверджує оператор. BH-177: доти було лише «Оплата», і
#: повернення, яке емулятор навчився проводити, виглядало б у консолі як
#: списання — тобто людина підтверджувала б не те, що думає.
KIND_LABELS = {
    "purchase": "Оплата",
    "refund": "ПОВЕРНЕННЯ",
    "void": "СКАСУВАННЯ оплати",
    "partial_void": "ЧАСТКОВЕ скасування",
}


#: Merchant counts the emulated terminal can be started with.
MERCHANT_COUNT_CHOICES = (1, 2)

DEFAULT_MERCHANT_ID = "00000012345"
DEFAULT_MERCHANT_NAME = "EMULATOR MERCHANT"
DEFAULT_TERMINAL_ID = "EMU00001"


@dataclass(frozen=True)
class Merchant:
    """One merchant configured on the emulated terminal. A real terminal can
    carry more than one, and every protocol reports them as a list."""

    merchant_id: str
    merchant_name: str
    terminal_id: str


def _step_id(value: str, position: int) -> str:
    """`value` with its trailing number moved up by `position - 1`."""
    digits = len(value) - len(value.rstrip("0123456789"))
    if digits == 0:
        return f"{value}-{position}"
    head, tail = value[:-digits], value[-digits:]
    return f"{head}{int(tail) + position - 1:0{digits}d}"


def build_merchants(
    count: int = 1,
    *,
    merchant_id: str = DEFAULT_MERCHANT_ID,
    merchant_name: str = DEFAULT_MERCHANT_NAME,
    terminal_id: str = DEFAULT_TERMINAL_ID,
) -> list["Merchant"]:
    """The merchant list the emulated terminal reports.

    The first entry is the one named by `merchant_id` / `merchant_name` /
    `terminal_id`. Every further entry is derived from it: the trailing
    number of the id and of the terminal id steps up and the name carries
    its position, so the entries are told apart by every field.
    """
    if count not in MERCHANT_COUNT_CHOICES:
        raise ValueError(
            f"merchant count must be one of {MERCHANT_COUNT_CHOICES}, got {count!r}",
        )
    merchants = [Merchant(
        merchant_id=merchant_id,
        merchant_name=merchant_name,
        terminal_id=terminal_id,
    )]
    for position in range(2, count + 1):
        merchants.append(Merchant(
            merchant_id=_step_id(merchant_id, position),
            merchant_name=f"{merchant_name} {position}",
            terminal_id=_step_id(terminal_id, position),
        ))
    return merchants


@dataclass
class Pending:
    """One in-flight operation handed from the server thread to the console."""

    amount_kopecks: int
    currency: str
    event: threading.Event = field(default_factory=threading.Event)
    decision: str = "a"  # 'a' approve | 'd' decline | 'c' cancel
    kind: str = "purchase"  # див. KIND_LABELS
    #: rrn / номер чека, за яким повертають — щоб оператор бачив, ЩО саме
    reference: str = ""

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)


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
        merchant_id: str = DEFAULT_MERCHANT_ID,
        merchant_name: str = DEFAULT_MERCHANT_NAME,
        terminal_id: str = DEFAULT_TERMINAL_ID,
        merchant_count: int = 1,
        merchants: Optional[Sequence[Merchant]] = None,
        model: str = "BARHANDLER EMULATOR",
        serial: str = "EMU-0001",
        on_traffic: Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        self.decisions = decisions
        self.merchants: list[Merchant] = (
            list(merchants) if merchants else build_merchants(
                merchant_count,
                merchant_id=merchant_id,
                merchant_name=merchant_name,
                terminal_id=terminal_id,
            )
        )
        self.model = model
        self.serial = serial
        self.on_traffic = on_traffic or (lambda direction, msg: None)
        self.current: Optional[Pending] = None

    # -- console handoff ---------------------------------------------------

    def _await_decision(
        self,
        amount_kopecks: int,
        currency: str,
        *,
        kind: str = "purchase",
        reference: str = "",
    ) -> str:
        """Block (in the executor thread) until the console operator picks an
        outcome for this operation. Returns 'a' | 'd' | 'c'."""
        pending = Pending(
            amount_kopecks=amount_kopecks, currency=currency,
            kind=kind, reference=reference,
        )
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
