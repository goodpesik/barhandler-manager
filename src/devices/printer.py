"""Generic ESC/POS printer device.

Wraps python-escpos so the rest of the codebase doesn't have to care
whether the printer is USB or network-attached, and so we can serialise
print jobs through an asyncio FIFO queue per device (the response to
POST /print/receipt blocks until that job has physically printed).

Any printer that speaks ESC/POS will work — we never hardcode a model.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Awaitable, Callable, Optional

from escpos.printer import Network, Usb

logger = logging.getLogger(__name__)


class PrinterUnavailable(RuntimeError):
    """Raised when a print job is attempted but the printer is offline."""


class PrinterDevice:
    """A single ESC/POS printer (USB or network) with a FIFO job queue."""

    def __init__(self, name: str, config: dict) -> None:
        self.name = name
        self._config = config or {}
        self._printer = None
        self._queue: "asyncio.Queue[tuple[Callable[[object], Awaitable[None]], asyncio.Future]]" = (
            asyncio.Queue()
        )
        self._worker_task: Optional[asyncio.Task] = None

    # ----- lifecycle ---------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled"))

    @property
    def paper_width(self) -> int:
        return int(self._config.get("paper_width") or 58)

    @property
    def chars_per_line(self) -> int:
        return 48 if self.paper_width >= 80 else 32

    @property
    def code_page(self) -> str:
        return self._config.get("code_page") or "cp866"

    @property
    def drawer_pin(self):
        return self._config.get("drawer_pin")

    def is_connected(self) -> bool:
        return self._printer is not None

    async def connect(self) -> bool:
        """Try to open the device. Returns True on success, False otherwise.

        Never raises — printer absence is a normal runtime state (the
        web app polls /health and shows a modal). The worker is spawned
        only when we actually have a printer to drive.
        """
        if not self.enabled:
            return False
        try:
            self._printer = self._build_printer()
        except Exception as exc:  # noqa: BLE001 — pyusb/escpos can throw anything
            logger.warning("[%s] connect failed: %s", self.name, exc)
            self._printer = None
            return False
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name=f"printer-{self.name}")
        logger.info("[%s] connected (%s)", self.name, self._config.get("connection"))
        return True

    async def disconnect(self) -> None:
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
        if self._printer is not None:
            with suppress(Exception):
                self._printer.close()
            self._printer = None

    # ----- queue interface ---------------------------------------------

    async def enqueue(self, job: Callable[[object], Awaitable[None]]):
        """Submit a job and wait until it has been executed by the worker.

        `job` is `async def fn(printer): ...` — it receives the live
        python-escpos printer instance and is free to issue any commands.
        Raises PrinterUnavailable if the device isn't connected.
        """
        if not self.is_connected():
            raise PrinterUnavailable(f"{self.name} printer is not connected")
        loop = asyncio.get_running_loop()
        done: asyncio.Future = loop.create_future()
        await self._queue.put((job, done))
        return await done

    async def open_drawer(self) -> None:
        """Pulse the cash-drawer connector. Silent no-op when disabled."""
        if self.drawer_pin is None or not self.is_connected():
            return  # graceful — most setups have no drawer
        pin = int(self.drawer_pin)

        async def _job(printer):
            printer.cashdraw(pin)

        await self.enqueue(_job)

    # ----- internals ---------------------------------------------------

    def _build_printer(self):
        connection = (self._config.get("connection") or "usb").lower()
        if connection == "network":
            host = self._config.get("host")
            port = int(self._config.get("port") or 9100)
            if not host:
                raise ValueError("network printer requires host")
            return Network(host=host, port=port, profile=self._config.get("profile"))

        # USB — PrinterRegistry supplies fully-resolved vendor/product/
        # endpoints from a discovered descriptor, so we never need to scan
        # here. Missing values are a configuration bug, not a runtime
        # condition.
        vendor_id = self._coerce_hex(self._config.get("vendor_id"))
        product_id = self._coerce_hex(self._config.get("product_id"))
        in_ep = self._coerce_hex(self._config.get("in_ep"))
        out_ep = self._coerce_hex(self._config.get("out_ep"))
        if vendor_id is None or product_id is None or in_ep is None or out_ep is None:
            raise ValueError(
                "usb printer requires vendor_id, product_id, in_ep and out_ep — "
                "register the printer via POST /devices/register first"
            )
        return Usb(
            idVendor=vendor_id,
            idProduct=product_id,
            in_ep=in_ep,
            out_ep=out_ep,
            profile=self._config.get("profile"),
        )

    @staticmethod
    def _coerce_hex(value):
        if value is None:
            return None
        if isinstance(value, int):
            return value
        s = str(value).strip()
        return int(s, 16) if s.lower().startswith("0x") else int(s)

    async def _worker(self) -> None:
        while True:
            job, done = await self._queue.get()
            try:
                # Let python-escpos' MagicEncode pick the right code page per
                # character — calling `charcode()` manually disables that.
                # If the printer keeps printing '?' for Ukrainian letters,
                # the printer firmware lacks the required page; switch to
                # a specific page via `code_page` config (cp866/cp1251).
                if self._config.get("code_page"):
                    with suppress(Exception):
                        self._printer.magic.force_encoding(self.code_page)
                await job(self._printer)
                if not done.done():
                    done.set_result(None)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[%s] print job failed", self.name)
                if not done.done():
                    done.set_exception(exc)
            finally:
                self._queue.task_done()
