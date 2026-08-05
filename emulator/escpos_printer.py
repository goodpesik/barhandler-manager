"""ESC/POS thermal-printer emulator — the device side of the wire.

Listens on a raw TCP port (JetDirect / RAW 9100) exactly like a network
thermal printer, so barhandler-manager's python-escpos ``Network`` driver
(``src/devices/printer.py``) prints to it with **zero** manager-side
changes. We reconstruct, pixel-for-pixel, the bitmap a real thermal head
would burn — then hand it to the live web viewer as a PNG.

Why this is simple: in the manager's default **bitmap** render mode every
glyph, line and even the fiscal QR is rasterised through PIL and emitted
as a single ``GS v 0`` raster command (see
``src/services/bitmap_render.image_to_gs_v_0`` and
``src/services/fiscal_receipt``). So the *only* image command we ever see
on the wire is ``GS v 0``. Everything else is ESC/POS control bytes we
skip with the right lengths, plus raw code-page text for the ``native``
render mode (decoded best-effort for the preview).

Two paper widths are supported, exactly the manager's two modes:
    58mm → 384 dots,  80mm → 576 dots
The width is *self-describing* — it's encoded in every ``GS v 0`` header —
so the emulator auto-detects which mode the manager is driving.

One must-have for the round-trip: before every job the manager calls
``check_status()`` which sends the real-time queries ``DLE EOT 1``
(online) and ``DLE EOT 4`` (paper) and blocks on ``recv(16)``. If we stay
silent ``is_online()`` returns False and the manager refuses to print. We
answer a single byte ``0x12`` → decodes to *online + paper adequate* in
python-escpos (RT_MASK_ONLINE bit clear, RT_MASK_PAPER bits set).

Local test tool only. Not imported by the manager app, not wired into any
route.
"""

from __future__ import annotations

import asyncio
import io
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("emulator.printer")

# Paper geometry — mirrors src/services/bitmap_render.PAPER_DOTS.
PAPER_DOTS = {58: 384, 80: 576}
DOTS_TO_MM = {384: 58, 576: 80}

# Same font + base size the manager rasterises with, so the native-mode
# text fallback looks like the real receipts. Lives in src/assets/fonts.
_FONTS_DIR = Path(__file__).resolve().parent.parent / "src" / "assets" / "fonts"
_FONT_REGULAR = _FONTS_DIR / "NotoSansMono-Regular.ttf"
_FONT_BOLD = _FONTS_DIR / "NotoSansMono-Bold.ttf"
_BASE_FONT_PX = 20

# Status reply: 0x12 = 0b00010010. RT_MASK_ONLINE (0x08) is clear → online;
# RT_MASK_PAPER (0x12) bits set, RT_MASK_NOPAPER/LOWPAPER not matched → "ok".
_STATUS_OK = b"\x12"

# ESC t <table> → Python codec, for decoding native-mode text in the preview.
_CODEPAGE_CODEC = {
    0: "cp437",
    17: "cp866",   # table 17 — what encode_ua_cp866 selects
    33: "cp1251",
    46: "cp1251",
    44: "cp1125",
    34: "cp855",
    38: "iso8859_5",
}


def dots_to_mm(width_px: int) -> int:
    return DOTS_TO_MM.get(width_px, max(1, round(width_px / 8)))


# ---------------------------------------------------------------------------
# Shared, thread-safe store of rendered receipts (kept in memory — we never
# litter the disk with PNG files; the web viewer reads them straight from RAM)
# ---------------------------------------------------------------------------


@dataclass
class Receipt:
    id: int
    png: bytes
    width: int
    height: int
    paper_mm: int
    ts: float


class PrinterState:
    """Holds the last `keep` receipts as in-memory PNGs + a notify queue."""

    def __init__(self, keep: int = 50) -> None:
        self._lock = threading.Lock()
        self._receipts: list[Receipt] = []
        self._counter = 0
        self._keep = keep
        # Console notifications for the main thread (mirrors the terminal
        # emulator's decisions queue).
        self.notifications: "list" = []
        self._notify: Optional[Callable[[Receipt], None]] = None

    def on_notify(self, cb: Callable[[Receipt], None]) -> None:
        self._notify = cb

    def add(self, png: bytes, width: int, height: int) -> Receipt:
        with self._lock:
            self._counter += 1
            receipt = Receipt(
                id=self._counter,
                png=png,
                width=width,
                height=height,
                paper_mm=dots_to_mm(width),
                ts=time.time(),
            )
            self._receipts.append(receipt)
            if len(self._receipts) > self._keep:
                self._receipts = self._receipts[-self._keep :]
        if self._notify is not None:
            try:
                self._notify(receipt)
            except Exception:  # noqa: BLE001 — never let the console kill a print
                log.exception("notify callback failed")
        return receipt

    def snapshot(self) -> list[Receipt]:
        with self._lock:
            return list(reversed(self._receipts))  # newest first

    def get(self, receipt_id: int) -> Optional[Receipt]:
        with self._lock:
            for r in self._receipts:
                if r.id == receipt_id:
                    return r
        return None


# ---------------------------------------------------------------------------
# Font cache for the native-mode text fallback
# ---------------------------------------------------------------------------

_font_cache: dict[tuple[bool, int], ImageFont.FreeTypeFont] = {}


def _font(*, bold: bool, scale: float) -> ImageFont.FreeTypeFont:
    size = max(8, int(_BASE_FONT_PX * scale))
    key = (bold, size)
    cached = _font_cache.get(key)
    if cached is None:
        path = _FONT_BOLD if bold else _FONT_REGULAR
        cached = ImageFont.truetype(str(path), size)
        _font_cache[key] = cached
    return cached


# ---------------------------------------------------------------------------
# Streaming ESC/POS interpreter — one per TCP connection
# ---------------------------------------------------------------------------


@dataclass
class _TextStyle:
    bold: bool = False
    align: str = "left"          # left | center | right
    double_height: bool = False
    double_width: bool = False


class EscPosInterpreter:
    """Parse an ESC/POS byte stream into a stack of receipt strips.

    `feed()` is called with whatever the socket delivers (possibly a
    partial command at the tail — we keep the remainder buffered). Each
    paper cut (``GS V``) finalises the accumulated strips into one PNG and
    fires `on_receipt`. Real-time status queries get an immediate reply,
    returned from `feed()` for the caller to write back on the socket.
    """

    def __init__(
        self,
        *,
        on_receipt: Callable[[bytes, int, int], None],
        default_width: int = 384,
    ) -> None:
        self._on_receipt = on_receipt
        self._default_width = default_width
        self._buf = bytearray()
        # Heterogeneous op list, rendered to a canvas at cut time once the
        # final width is known: ('raster', img) | ('text', str, style) |
        # ('gap', px).
        self._ops: list[tuple] = []
        self._text: list[str] = []          # current (unterminated) text line
        self._style = _TextStyle()
        self._observed_width = 0            # widest GS v 0 raster seen
        self._codec = "cp866"

    # -- public ------------------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        responses = bytearray()
        i = 0
        n = len(self._buf)
        while i < n:
            b = self._buf[i]
            if b == 0x1B:                    # ESC
                consumed = self._handle_esc(i, n)
            elif b == 0x1D:                  # GS
                consumed = self._handle_gs(i, n)
            elif b == 0x10:                  # DLE — real-time status
                consumed = self._handle_dle(i, n, responses)
            elif b == 0x0A:                  # LF — end of text line
                # In bitmap mode every line is `GS v 0` + `\n`, so a bare LF
                # right after a raster is just that raster's terminator — it
                # must NOT add a blank line (that was the source of the big
                # uniform gaps). Genuine blank lines arrive as blank rasters.
                if not self._text and self._ops and self._ops[-1][0] == "raster":
                    pass
                else:
                    self._flush_text_line(blank=True)
                consumed = 1
            elif b == 0x0D:                  # CR — ignore
                consumed = 1
            elif b == 0x09:                  # HT — tab → 4 spaces
                self._text.append("    ")
                consumed = 1
            elif b == 0x00:                  # NUL padding — ignore
                consumed = 1
            else:                            # printable / code-page byte
                consumed = self._handle_text_byte(i)
            if consumed == 0:                # need more bytes — wait
                break
            i += consumed
        del self._buf[:i]
        return bytes(responses)

    def close(self) -> None:
        """Connection ended. Flush any un-cut content as a final receipt
        (covers an aborted job); silent if nothing was printed so the
        manager's liveness probes don't spawn blank receipts."""
        self._finalize()

    # -- ESC dispatch ------------------------------------------------------

    def _handle_esc(self, i: int, n: int) -> int:
        if i + 1 >= n:
            return 0
        cmd = self._buf[i + 1]
        # (command byte, total length, optional handler)
        if cmd == 0x40:                      # ESC @ — initialise / reset
            self._style = _TextStyle()
            return 2
        if cmd == 0x61:                      # ESC a n — text alignment
            if i + 2 >= n:
                return 0
            self._flush_text_line()
            self._style.align = {0: "left", 1: "center", 2: "right"}.get(
                self._buf[i + 2], "left"
            )
            return 3
        if cmd == 0x45:                      # ESC E n — emphasise (bold)
            if i + 2 >= n:
                return 0
            self._flush_text_line()
            self._style.bold = bool(self._buf[i + 2])
            return 3
        if cmd == 0x21:                      # ESC ! n — print mode bits
            if i + 2 >= n:
                return 0
            self._flush_text_line()
            mode = self._buf[i + 2]
            self._style.bold = bool(mode & 0x08)
            self._style.double_height = bool(mode & 0x10)
            self._style.double_width = bool(mode & 0x20)
            return 3
        if cmd == 0x74:                      # ESC t n — code page table
            if i + 2 >= n:
                return 0
            self._codec = _CODEPAGE_CODEC.get(self._buf[i + 2], self._codec)
            return 3
        if cmd == 0x64:                      # ESC d n — feed n lines
            if i + 2 >= n:
                return 0
            self._flush_text_line()
            self._ops.append(("gap", max(1, self._buf[i + 2]) * 6))
            return 3
        if cmd == 0x4A:                      # ESC J n — feed n dots
            if i + 2 >= n:
                return 0
            self._ops.append(("gap", self._buf[i + 2]))
            return 3
        if cmd == 0x70:                      # ESC p m t1 t2 — kick drawer
            if i + 4 >= n:
                return 0
            log.info("cash drawer kicked (ESC p)")
            return 5
        # Known one-parameter ESC commands we don't render (spacing, font,
        # underline, double-strike, rotation, upside-down, …).
        if cmd in (0x32,):                   # ESC 2 — default line spacing
            return 2
        if cmd in (0x33, 0x4D, 0x47, 0x2D, 0x7B, 0x52, 0x56, 0x20, 0x63):
            if i + 2 >= n:
                return 0
            return 3
        # Unknown ESC command — skip ESC + cmd byte and hope it had no
        # parameter. Bitmap mode never emits these, so this is just defence.
        log.debug("unhandled ESC 0x%02x", cmd)
        return 2

    # -- GS dispatch -------------------------------------------------------

    def _handle_gs(self, i: int, n: int) -> int:
        if i + 1 >= n:
            return 0
        cmd = self._buf[i + 1]
        if cmd == 0x76:                      # GS v 0 — raster bit image
            return self._handle_raster(i, n)
        if cmd == 0x56:                      # GS V — cut paper
            if i + 2 >= n:
                return 0
            m = self._buf[i + 2]
            if m in (65, 66):                # GS V 65/66 n — feed then cut
                if i + 3 >= n:
                    return 0
                self._finalize()
                return 4
            self._finalize()                 # GS V 0 / 1
            return 3
        if cmd == 0x21:                      # GS ! n — character size
            if i + 2 >= n:
                return 0
            self._flush_text_line()
            size = self._buf[i + 2]
            self._style.double_width = bool((size >> 4) & 0x0F)
            self._style.double_height = bool(size & 0x0F)
            return 3
        if cmd in (0x42,):                   # GS B n — white/black reverse
            if i + 2 >= n:
                return 0
            return 3
        if cmd in (0x4C, 0x57):              # GS L / GS W — margins (2 args)
            if i + 3 >= n:
                return 0
            return 4
        if cmd in (0x68, 0x77, 0x48, 0x66, 0x72, 0x61):  # barcode setup (1 arg)
            if i + 2 >= n:
                return 0
            return 3
        log.debug("unhandled GS 0x%02x", cmd)
        return 2

    def _handle_raster(self, i: int, n: int) -> int:
        # GS v 0 m xL xH yL yH [data]
        if i + 8 > n:
            return 0
        bytes_per_row = self._buf[i + 4] + (self._buf[i + 5] << 8)
        height = self._buf[i + 6] + (self._buf[i + 7] << 8)
        data_len = bytes_per_row * height
        end = i + 8 + data_len
        if end > n:                          # raster body not fully arrived
            return 0
        data = bytes(self._buf[i + 8 : end])
        width_px = bytes_per_row * 8
        # Wire packs 1 = black (MSB-first). PIL '1' treats bit 1 = white, so
        # invert to land black pixels on black.
        inverted = bytes(byte ^ 0xFF for byte in data)
        img = Image.frombytes("1", (width_px, height), inverted)
        self._observed_width = max(self._observed_width, width_px)
        self._ops.append(("raster", img))
        return 8 + data_len

    # -- DLE (real-time status) -------------------------------------------

    def _handle_dle(self, i: int, n: int, responses: bytearray) -> int:
        if i + 1 >= n:
            return 0
        cmd = self._buf[i + 1]
        if cmd == 0x04:                      # DLE EOT n — transmit status
            if i + 2 >= n:
                return 0
            responses += _STATUS_OK
            return 3
        if cmd == 0x14:                      # DLE DC4 fn m t — real-time req
            if i + 4 >= n:
                return 0
            return 5
        return 1                             # lone DLE — skip

    # -- text mode ---------------------------------------------------------

    def _handle_text_byte(self, i: int) -> int:
        self._text.append(chr(self._buf[i]))
        return 1

    def _flush_text_line(self, *, blank: bool = False) -> None:
        """Render the pending text line. `blank=True` (an LF) emits an empty
        strip for a genuine blank line; otherwise (a style change mid-line)
        nothing is emitted when the buffer is empty — so a native-mode
        `set(); text()` sequence doesn't inject spurious blank lines."""
        if not self._text:
            if blank:
                self._ops.append(("text", "", _TextStyle(**vars(self._style))))
            return
        raw = "".join(self._text).encode("latin-1", errors="replace")
        try:
            text = raw.decode(self._codec, errors="replace")
        except LookupError:
            text = raw.decode("cp866", errors="replace")
        self._text = []
        self._ops.append(("text", text, _TextStyle(**vars(self._style))))

    # -- finalise ----------------------------------------------------------

    def _finalize(self) -> None:
        self._flush_text_line() if self._text else None
        # Drop trailing empty text lines (the manager pads with \n before a
        # cut) so receipts don't carry a tail of blank space.
        ops = list(self._ops)
        while ops and ops[-1][0] == "text" and not ops[-1][1]:
            ops.pop()
        self._ops = []
        if not ops:
            return                           # liveness probe / empty job
        width = self._observed_width or self._default_width
        self._observed_width = 0
        png, w, h = self._render(ops, width)
        self._on_receipt(png, w, h)

    def _render(self, ops: list[tuple], width: int) -> tuple[bytes, int, int]:
        margin = 16
        strips: list[Image.Image] = []
        for op in ops:
            if op[0] == "raster":
                strips.append(self._fit(op[1], width))
            elif op[0] == "gap":
                strips.append(Image.new("1", (width, max(1, int(op[1]))), 1))
            elif op[0] == "text":
                strips.append(self._render_text(op[1], op[2], width))
        body_h = sum(s.height for s in strips)
        total_h = body_h + margin * 2
        canvas = Image.new("1", (width, total_h), 1)
        y = margin
        for s in strips:
            canvas.paste(s, (0, y))
            y += s.height
        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        return buf.getvalue(), width, total_h

    @staticmethod
    def _fit(img: Image.Image, width: int) -> Image.Image:
        if img.width == width:
            return img
        out = Image.new("1", (width, img.height), 1)
        out.paste(img, (0, 0))               # left-align, pad/crop to width
        return out

    def _render_text(self, text: str, style: _TextStyle, width: int) -> Image.Image:
        font = _font(bold=style.bold, scale=1.0)
        ascent, descent = font.getmetrics()
        pad_y = 2
        line_h = ascent + descent + pad_y * 2
        canvas = Image.new("1", (width, line_h), 1)
        if text:
            draw = ImageDraw.Draw(canvas)
            bbox = font.getbbox(text)
            text_w = bbox[2] - bbox[0]
            if style.align == "right":
                x = max(0, width - text_w)
            elif style.align == "center":
                x = max(0, (width - text_w) // 2)
            else:
                x = 0
            draw.text((x, pad_y), text, font=font, fill=0)
        if style.double_height:
            canvas = canvas.resize((canvas.width, canvas.height * 2))
        return canvas


# ---------------------------------------------------------------------------
# asyncio TCP server (raw ESC/POS sink, JetDirect/RAW 9100)
# ---------------------------------------------------------------------------


async def _serve_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    state: PrinterState,
    default_width: int,
) -> None:
    peer = writer.get_extra_info("peername")

    def _on_receipt(png: bytes, w: int, h: int) -> None:
        state.add(png, w, h)

    interp = EscPosInterpreter(on_receipt=_on_receipt, default_width=default_width)
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            response = interp.feed(chunk)
            if response:
                writer.write(response)
                await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        interp.close()
        with _suppress():
            writer.close()
            await writer.wait_closed()
    log.debug("connection from %s closed", peer)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def bind_raw_socket(host: str, port: int, *, span: int = 20) -> socket.socket:
    """Bind a listening TCP socket on ``host:port``, falling back to the next
    free port (``port+1 … port+span``) when the desired one is already taken.

    Why: both emulators default to RAW/9100, and the manager's LAN discovery
    scans a small 9100+ range — so running the receipt and label emulators
    together on one host used to leave the second one's RAW sink silently
    dead (``asyncio.start_server`` raised EADDRINUSE inside its daemon thread).
    Now it just steps to the next port and stays discoverable.

    ``SO_REUSEADDR`` only smooths TIME_WAIT reuse across quick restarts — it
    does NOT let two live listeners share a port, so an already-serving
    emulator still correctly bumps us to the next one. Returns the bound,
    listening socket to hand to ``asyncio.start_server(sock=...)``.
    """
    last_exc: Optional[OSError] = None
    for candidate in range(port, port + span + 1):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, candidate))
            s.listen(128)
            s.setblocking(False)
            return s
        except OSError as exc:
            last_exc = exc
            s.close()
    raise OSError(
        f"no free port in {port}..{port + span} on {host!r}: {last_exc}"
    )


async def run_server(
    sock: socket.socket, state: PrinterState, default_width: int
) -> None:
    server = await asyncio.start_server(
        lambda r, w: _serve_client(r, w, state, default_width), sock=sock
    )
    async with server:
        await server.serve_forever()


def start_server_thread(
    host: str, port: int, state: PrinterState, default_width: int
) -> int:
    """Bind the RAW sink (with port fallback) and run the asyncio ESC/POS
    server on its own loop in a daemon thread so the main thread is free to
    own the console (mirrors ssi_terminal). Returns the ACTUAL bound port,
    which may differ from ``port`` when 9100 was already taken."""
    sock = bind_raw_socket(host, port)
    actual_port = sock.getsockname()[1]

    def _run() -> None:
        asyncio.run(run_server(sock, state, default_width))

    thread = threading.Thread(target=_run, name="printer-emulator-server", daemon=True)
    thread.start()
    return actual_port
