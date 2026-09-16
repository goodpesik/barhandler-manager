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

from src.services.bitmap_render import dots_for, image_to_gs_v_0, render_paragraph
from src.services.tspl_render import image_to_tspl_bitmap
from src.services.encoding import encode_ua_cp866

logger = logging.getLogger(__name__)


class PrinterUnavailable(RuntimeError):
    """Raised when a print job is attempted but the printer is offline.

    `code` carries a stable identifier the frontend can branch on
    (e.g. `out_of_paper`) so the operator gets a specific message
    instead of a generic "printer error" toast. `code` defaults to
    `unavailable` when the underlying cause isn't a query-able state
    (USB disconnected mid-print, manager itself died, etc).
    """

    def __init__(self, message: str, code: str = "unavailable") -> None:
        super().__init__(message)
        self.code = code


# Стеля висоти одного TSPL-джоба в точках. 2540 точок = 317 мм при 8 точках/мм
# — типова межа довжини етикетки в дешевих TSPL-прошивках. Вищий чек ріжеться
# на кілька послідовних `BITMAP`/`PRINT`, які на суцільній стрічці лягають
# упритул. Розріз проходить МІЖ рядками, не крізь них — див. `_finalize_tspl`.
#
# Окремий рядок, вищий за стелю, лишається цілим: краще один завеликий джоб,
# який прошивка, можливо, і подужає, ніж гарантовано розрізана літера.
_TSPL_MAX_DOTS = 2540


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
        # BH-160 — зібрані рядки одного TSPL-джоба. Порожньо між джобами.
        self._tspl_pages: list = []

    # ----- lifecycle ---------------------------------------------------

    def _is_tspl(self) -> bool:
        """Чи говорить цей пристрій мовою TSPL.

        Протокол — властивість ПРИСТРОЮ, не ролі. Доти чековий шлях завжди
        слав ESC/POS, і на етикетковому залізі це означало тишу: прошивка
        приймає байти й мовчки їх викидає.
        """
        return str(self._config.get("protocol") or "escpos").lower() == "tspl"

    def _finalize_tspl(self) -> None:
        """Віддати зібрані рядки одним TSPL-джобом і очистити накопичувач.

        Чек — це не етикетка фіксованої висоти: висота рахується з того, що
        вийшло. `GAP 0 mm, 0 mm` каже «стрічка суцільна» — з ненульовим
        зазором принтер шукав би проміжок, якого немає, і гнав би порожнє.

        Викликається В БУДЬ-ЯКОМУ разі — і коли джоб відпрацював, і коли
        впав: інакше рядки впалого джоба лишились би в накопичувачі й
        вилізли б зверху наступного чека.
        """
        pages, self._tspl_pages = self._tspl_pages, []
        if not pages or self._printer is None:
            return

        from PIL import Image

        width = max(img.width for img in pages)

        # Ріжемо по МЕЖАХ РЯДКІВ, а не по сирих точках.
        #
        # Знайдено другим колом ревʼю: розріз за кратністю стелі міг лягти
        # ПОСЕРЕД рядка тексту, і тоді літера виявлялась розділеною між двома
        # окремими `PRINT`. На ідеально рівній подачі цього не видно, але два
        # послідовні проходи по суцільній стрічці неминуче мають люфт кроку —
        # і шов проходить зазубриною просто крізь рядок. Рядок цілий у своєму
        # шматку — і люфт лягає в міжрядковий проміжок, де його не видно.
        groups: list[list] = [[]]
        group_height = 0
        for img in pages:
            if group_height and group_height + img.height > _TSPL_MAX_DOTS:
                groups.append([])
                group_height = 0
            groups[-1].append(img)
            group_height += img.height

        for group in groups:
            if not group:
                continue
            height = sum(img.height for img in group)
            canvas = Image.new("1", (width, height), 1)  # 1 = білий у режимі "1"
            y = 0
            for img in group:
                canvas.paste(img, (0, y))
                y += img.height
            self._emit_tspl_canvas(canvas)

    def _emit_tspl_canvas(self, canvas) -> None:
        """Віддати один готовий бітмап як `SIZE / GAP / BITMAP / PRINT`."""

        # 203 dpi = 8 точок/мм — та сама пітч, з якої рахує `dots_for`.
        height_mm = max(1, -(-canvas.height // 8))
        self._printer._raw(image_to_tspl_bitmap(
            canvas,
            label_width_mm=self.paper_width,
            label_height_mm=height_mm,
            gap_mm=0,
            copies=1,
        ))

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

    async def async_probe(self) -> bool:
        """Non-destructive liveness check. Returns False if the physical
        device is unreachable so the heartbeat can clear the stale handle.

        Network: opens a fresh TCP connection (2s timeout) and closes it.
        USB: calls is_online() on the existing handle.
        """
        if self._printer is None:
            return False
        connection = (self._config.get("connection") or "usb").lower()
        if connection == "network":
            host = self._config.get("host")
            port = int(self._config.get("port") or 9100)
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=2.0,
                )
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                return True
            except Exception:
                return False
        else:
            try:
                return bool(self._printer.is_online())
            except Exception:
                return False

    def check_status(self) -> dict:
        """Best-effort real-time status query (ESC/POS DLE EOT n series).

        Cheap ESC/POS clones often ignore the status command — when they
        do, python-escpos returns an empty buffer and reports "paper OK
        / online" by default. That's a lying answer but the only one we
        can give, so we surface `supported: false` for the caller that
        wants to differentiate "actively healthy" from "didn't answer".

        Worker calls this before every job; if the printer truthfully
        reports `out_of_paper` we fail fast with a code the frontend can
        show ("Закінчився папір") instead of letting the print silently
        produce a blank ribbon.
        """
        if not self.is_connected():
            return {"online": False, "paper": "unknown", "supported": False}
        try:
            paper_raw = self._printer.paper_status()  # 0=empty, 1=low, 2=ok
            online = self._printer.is_online()
        except Exception as exc:  # noqa: BLE001 — vendor-specific exceptions vary
            logger.debug("[%s] status query failed: %s", self.name, exc)
            return {"online": True, "paper": "unknown", "supported": False}
        return {
            "online": bool(online),
            "paper": {0: "empty", 1: "low", 2: "ok"}.get(paper_raw, "unknown"),
            "supported": True,
        }

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
        self._fail_queued_jobs()
        if self._printer is not None:
            with suppress(Exception):
                self._printer.close()
            self._printer = None

    def _reset_render_state(self) -> None:
        """Прибрати за джобом: зібрані TSPL-сторінки і недописаний рядок."""
        self._tspl_pages.clear()
        reset = getattr(self._printer, "_bh_reset_render_state", None)
        if callable(reset):
            with suppress(Exception):
                reset()

    def _fail_queued_jobs(self) -> None:
        """Розбудити всіх, хто чекає на джоби, які вже не виконаються.

        Знайдено ревʼю: скасований воркер лишав черзі невиконані елементи, а
        кожен із них — це HTTP-запит, що `await`-ить свій `done`. Без цього
        вони висять до таймауту клієнта й не бачать жодної помилки.
        """
        while True:
            try:
                _job, done = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not done.done():
                done.set_exception(
                    PrinterUnavailable(
                        f"{self.name}: друк скасовано — принтер перенастроюють",
                        code="cancelled",
                    ),
                )
            self._queue.task_done()

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

    def _is_spooler(self) -> bool:
        return (self._config.get("connection") or "").lower() == "windows_spooler"

    def _build_printer(self):
        connection = (self._config.get("connection") or "usb").lower()
        if connection == "network":
            host = self._config.get("host")
            port = int(self._config.get("port") or 9100)
            if not host:
                raise ValueError("network printer requires host")
            return Network(host=host, port=port, profile=self._config.get("profile"))

        if connection == "windows_spooler":
            # Windows print spooler (win32print RAW). The printer is a normal
            # Windows printer (its own driver / Generic-Text) — no WinUSB, and
            # every other app can still print to it. We open a fresh spool
            # document per receipt in the worker (Win32Raw = one doc per
            # open/close), so we DON'T auto-open here.
            from escpos.printer import Win32Raw
            return Win32Raw(printer_name=self._config.get("printer_name") or "")

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

    def _install_bitmap_patch(self) -> None:
        """Replace text()/set() so every glyph is rasterised through PIL.

        State (bold / align / double-height / double-width) is tracked
        across set() calls and applied to the next text() render. We also
        buffer until we see a newline so each printed line ends up as a
        single image command instead of one per `text()` invocation.
        """
        printer = self._printer
        if getattr(printer, "_bh_bitmap_patched", False):
            return

        width_px = dots_for(self.paper_width)
        state = {
            "bold": False,
            "align": "left",
            "double_height": False,
            "double_width": False,
        }
        buffer: list[str] = []

        def flush() -> None:
            if not buffer:
                return
            chunk = "".join(buffer)
            buffer.clear()
            if not chunk:
                return
            scale_h = 2.0 if state["double_height"] else 1.0
            scale_w = 2.0 if state["double_width"] else 1.0
            for piece in chunk.split("\n")[:-1] + (
                [chunk.rsplit("\n", 1)[-1]] if not chunk.endswith("\n") else []
            ):
                img = render_paragraph(
                    piece,
                    width_px=width_px,
                    bold=state["bold"],
                    align=state["align"],
                    scale_height=scale_h,
                    scale_width=scale_w,
                )
                # Bypass python-escpos image() — write raw GS v 0 directly
                # so the printer's text/raster decoder boundaries stay
                # exactly where we want them.
                #
                # Do NOT emit a trailing "\n": GS v 0 already feeds the
                # paper by the raster's own height and returns the head to
                # the start of the line (ESC/POS spec). An extra LF makes
                # the firmware feed a *second* line at the default spacing
                # (~1/6"), which double-spaces every line — exactly the
                # loose look seen on the client's 80mm Xprinter. The
                # render already carries per-line padding, so consecutive
                # rasters stack tight on every printer. (Bottom margin is
                # handled by cut()'s print-and-feed, which is unaffected.)
                if self._is_tspl():
                    # BH-160 — TSPL друкує не рядками, а ЕТИКЕТКАМИ: `PRINT`
                    # на кожен рядок виплюнув би окрему етикетку на рядок.
                    # Тому копичимо рядки й віддаємо один високий бітмап у
                    # кінці джоба — див. `_finalize_tspl`.
                    self._tspl_pages.append(img)
                else:
                    printer._raw(image_to_gs_v_0(img))

        def text(s: str) -> None:
            if not s:
                return
            # Easy path — newline-terminated single line.
            s = str(s)
            buffer.append(s)
            if s.endswith("\n"):
                flush()

        def set_(**kwargs) -> None:
            # Flush whatever was buffered with the OLD state before
            # mutating, otherwise a `set(bold=True); text("X")` would
            # render X with whichever bold setting was current at flush.
            flush()
            if "align" in kwargs:
                state["align"] = kwargs["align"]
            if "bold" in kwargs:
                state["bold"] = bool(kwargs["bold"])
            if "double_height" in kwargs:
                state["double_height"] = bool(kwargs["double_height"])
            if "double_width" in kwargs:
                state["double_width"] = bool(kwargs["double_width"])

        original_cut = printer.cut

        def cut(*args, **kwargs):
            flush()
            if self._is_tspl():
                # Етикеткові принтери не мають ножа; команда розрізу для них
                # у кращому разі нічого не значить, у гіршому — заклинить
                # механізм. Стрічка відривається руками.
                return None
            return original_cut(*args, **kwargs)

        def reset() -> None:
            """Викинути недописаний рядок і повернути стан до типового.

            Знайдено ревʼю: `buffer` і `state` живуть у замиканні, яке
            ставиться ОДИН раз на пристрій, тож пережовують усі джоби. Якщо
            джоб упав після `text("Разом")` і до `\n`, цей хвіст мовчки
            приклеювався до першого рядка НАСТУПНОГО чека. На TSPL це гірше,
            ніж було: між джобами немає розрізу, тож чужий уламок стає
            частиною того самого документа.
            """
            buffer.clear()
            state.update({
                "bold": False,
                "align": "left",
                "double_height": False,
                "double_width": False,
            })

        printer.text = text
        printer.set = set_
        printer.cut = cut
        printer._bh_reset_render_state = reset
        printer._bh_bitmap_patched = True

    def _install_ua_text_patch(self) -> None:
        """Replace `printer.text` with one that emits raw UA-CP866 bytes.

        python-escpos' magic encoder doesn't know CP866-UA and would
        substitute '?' for every і/ї/є/ґ before sending. We swap the
        method once per device so every subsequent text() call inside a
        render goes through `encode_ua_cp866`.
        """
        printer = self._printer
        if getattr(printer, "_bh_ua_patched", False):
            return

        def text(s: str) -> None:
            printer._raw(encode_ua_cp866(str(s)))

        printer.text = text  # type: ignore[method-assign]
        printer._bh_ua_patched = True

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
            spooler = self._is_spooler()
            try:
                # Pre-flight status — bail out before sending bytes so a
                # paper-empty printer doesn't swallow an entire receipt
                # silently. The check is best-effort: cheap clones just
                # don't answer the status query and we proceed normally
                # (the operator hears the printer click and notices).
                # Skipped for the Windows spooler: it's write-only, real-time
                # status (DLE EOT) doesn't apply — the OS owns paper state.
                if not spooler:
                    status = self.check_status()
                    if status["supported"]:
                        if status["paper"] == "empty":
                            raise PrinterUnavailable(
                                f"{self.name}: paper is out", code="out_of_paper",
                            )
                        if not status["online"]:
                            # Most printers raise this when the cover is open
                            # or the head is parked; we can't tell the two
                            # apart from real-time status alone, but
                            # "cover_open" is the more actionable hint.
                            raise PrinterUnavailable(
                                f"{self.name}: not online (cover or head)",
                                code="cover_open",
                            )

                # Render mode selection:
                #   - "bitmap" (default): every text() call is rasterised
                #     through PIL and sent via printer.image(). Works on
                #     any ESC/POS printer with any Unicode input.
                #   - "native" + code_page == "ua_cp866": switch printer
                #     to table 17 and emit raw CP866-UA bytes (fast, but
                #     limited to printers with Ukrainian PC866 overlay).
                #   - "native" + other code_page: hand off to
                #     python-escpos magic.force_encoding().
                mode = (self._config.get("render_mode") or "bitmap").lower()
                code_page = (self.code_page or "").lower()
                if mode == "bitmap":
                    self._install_bitmap_patch()
                elif code_page == "ua_cp866":
                    with suppress(Exception):
                        self._printer._raw(b"\x1bt\x11")  # ESC t 17 = CP866
                    self._install_ua_text_patch()
                elif code_page:
                    with suppress(Exception):
                        self._printer.magic.force_encoding(self.code_page)
                if spooler:
                    # Open one spool document, run the whole receipt into it,
                    # then close so it flushes as a single Windows print job.
                    self._printer.open(job_name="barhandler", raise_not_found=True)
                    try:
                        await job(self._printer)
                        # Зібраний TSPL мусить лягти В ТОЙ САМИЙ спул-документ,
                        # тобто до close() — інакше він поїде окремим джобом
                        # або не поїде взагалі.
                        self._finalize_tspl()
                    finally:
                        self._reset_render_state()
                        with suppress(Exception):
                            self._printer.close()
                else:
                    try:
                        await job(self._printer)
                        self._finalize_tspl()
                    finally:
                        self._reset_render_state()
                if not done.done():
                    done.set_result(None)
            except OSError as exc:
                # Socket-level failure → the physical connection is gone.
                # Clear the handle so health() reports "unavailable" and
                # the next print attempt triggers a fresh connect().
                logger.warning("[%s] socket error — marking disconnected: %s", self.name, exc)
                with suppress(Exception):
                    if self._printer is not None:
                        self._printer.close()
                self._printer = None
                if not done.done():
                    done.set_exception(PrinterUnavailable(str(exc), code="unreachable"))
                return  # worker exits; registry.get_device() rebuilds on next print
            except asyncio.CancelledError:
                # Знайдено ревʼю. Воркер скасовують, коли пристрій викидають
                # із кеша — а це тепер трапляється на КОЖНІЙ перереєстрації,
                # не лише на видаленні. `CancelledError` — це BaseException,
                # тож повз `except OSError` і `except Exception` нижче він
                # летів мовчки, і `done` не отримував НІЧОГО: HTTP-запит, що
                # чекав на цей друк, висів до таймауту клієнта без жодної
                # помилки. Розбудити того, хто чекає, і лише тоді падати далі.
                if not done.done():
                    done.set_exception(
                        PrinterUnavailable(
                            f"{self.name}: друк перервано — принтер перенастроюють",
                            code="cancelled",
                        ),
                    )
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("[%s] print job failed", self.name)
                if not done.done():
                    done.set_exception(exc)
            finally:
                self._queue.task_done()
