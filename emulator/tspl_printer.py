"""TSPL label-printer emulator — the device side of the wire.

Listens on a raw TCP port (RAW/9100) like a network thermal **label**
printer, so barhandler-manager's label path (`/print/label` with
`protocol=tspl`) prints to it with zero manager-side changes. Where the
receipt emulator (`escpos_printer.py`) reconstructs `GS v 0` ESC/POS
rasters, this one reconstructs the **TSPL `BITMAP`** command the manager
emits (see `src/services/tspl_render.image_to_tspl_bitmap`) and hands the
finished label to the live web viewer as a PNG.

TSPL job on the wire (what we parse):

    SIZE <w> mm, <h> mm\\r\\n
    GAP  <g> mm, 0 mm\\r\\n
    DIRECTION 1\\r\\n
    CLS\\r\\n
    BITMAP 0,0,<bytes_per_row>,<height_px>,0,<binary…>\\r\\n
    PRINT <copies>\\r\\n

The tricky bit vs ESC/POS: the `BITMAP` binary payload can contain any
byte (including CR/LF/DLE), so the parser can't split on newlines — it
reads text commands line-by-line until it sees `BITMAP`, parses that
header up to the 5th comma, then consumes exactly `bytes_per_row ×
height` binary bytes before returning to line mode. `PRINT` finalises the
current label.

Bit packing: the manager sends PIL `"1"` bytes verbatim (1 = white,
0 = black — no inversion; see tspl_render), so we reconstruct with
`Image.frombytes("1", …)` as-is and the label reads right-way-up.

One round-trip detail: before a job the manager's PrinterDevice worker
calls `check_status()`, which sends ESC/POS `DLE EOT n`. Real TSPL
printers ignore it, but to keep `is_online()` happy we answer the same
single `0x12` byte the receipt emulator does.

Local test tool only. Not imported by the manager app.
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
from typing import Callable, Optional

from PIL import Image

# Reuse the receipt emulator's in-memory store + PNG record — a label is
# just a (small) Receipt whose paper_mm is its width. dots_to_mm's
# `round(width_px / 8)` fallback already yields mm at 8 dots/mm (e.g.
# 320 dots → 40 mm), so nothing label-specific is needed there.
from .escpos_printer import (  # noqa: F401
    PrinterState,
    Receipt,
    bind_raw_socket,
    dots_to_mm,
)

log = logging.getLogger("emulator.label")

DEFAULT_LABEL_DOTS = 320  # 40 mm @ 8 dots/mm — fallback when SIZE is absent
_STATUS_OK = b"\x12"      # online + paper adequate (matches escpos_printer)


class TsplInterpreter:
    """Parse a TSPL byte stream into finished labels.

    `feed()` takes whatever the socket delivers (partial commands / split
    binary payloads are buffered) and returns any bytes to write back
    (the `DLE EOT` status reply). Each `PRINT` fires `on_label(png, w, h)`.
    """

    def __init__(
        self,
        *,
        on_label: Callable[[bytes, int, int], None],
        default_width: int = DEFAULT_LABEL_DOTS,
    ) -> None:
        self._on_label = on_label
        self._default_width = default_width
        self._buf = bytearray()
        self._mode = "cmd"                 # "cmd" | "bitmap"
        self._need = 0                     # bytes of binary still to read
        self._bpr = 0
        self._height = 0
        self._size_mm: Optional[tuple[int, int]] = None
        self._current: Optional[Image.Image] = None

    # -- public ------------------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        resp = bytearray()
        while True:
            if self._mode == "bitmap":
                if len(self._buf) < self._need:
                    break
                raw = bytes(self._buf[: self._need])
                del self._buf[: self._need]
                width_px = self._bpr * 8
                # Wire bytes are PIL "1" packing (1 = white) — reconstruct
                # verbatim so black stays black.
                self._current = Image.frombytes("1", (width_px, self._height), raw)
                self._mode = "cmd"
                continue

            if not self._buf:
                break
            b0 = self._buf[0]

            if b0 == 0x10:                 # DLE — real-time status query
                if len(self._buf) < 3:
                    break
                if self._buf[1] == 0x04:   # DLE EOT n
                    resp += _STATUS_OK
                    del self._buf[:3]
                    continue
                del self._buf[:1]          # lone/unknown DLE — skip
                continue

            if b0 in (0x0D, 0x0A):         # stray CR/LF between commands
                del self._buf[:1]
                continue

            # BITMAP must be detected before any newline search — its
            # binary tail contains 0x0A bytes.
            if self._buf[:6].upper() == b"BITMAP":
                if not self._start_bitmap():
                    break                  # header not fully arrived yet
                continue

            idx = self._buf.find(0x0A)
            if idx == -1:
                break                      # partial text line — wait
            line = bytes(self._buf[:idx]).rstrip(b"\r")
            del self._buf[: idx + 1]
            self._handle_line(line)

        return bytes(resp)

    def close(self) -> None:
        """Connection ended — finalise a BITMAP that never got its PRINT
        (aborted job). Silent when there's nothing pending."""
        if self._current is not None:
            self._finalize(copies=1)

    # -- internals ---------------------------------------------------------

    def _start_bitmap(self) -> bool:
        """Parse `BITMAP x,y,bpr,h,mode,` up to the 5th comma. Returns
        False if the header hasn't fully arrived. On success, switches to
        binary mode for `bpr * h` bytes."""
        commas: list[int] = []
        for i, ch in enumerate(self._buf):
            if ch == 0x2C:                 # ','
                commas.append(i)
                if len(commas) == 5:
                    break
        if len(commas) < 5:
            return False
        header = bytes(self._buf[: commas[4]]).decode("ascii", "replace")
        del self._buf[: commas[4] + 1]     # consume through the 5th comma
        try:
            parts = header.split(",")
            bpr = int(parts[2])
            height = int(parts[3])
        except (ValueError, IndexError):
            log.debug("malformed BITMAP header: %r", header)
            return True                    # header consumed; ignore, stay in cmd
        self._bpr = bpr
        self._height = height
        self._need = bpr * height
        self._mode = "bitmap"
        return True

    def _handle_line(self, line: bytes) -> None:
        text = line.decode("ascii", "replace").strip()
        if not text:
            return
        upper = text.upper()
        if upper.startswith("SIZE"):
            self._size_mm = _parse_size(text)
        elif upper.startswith("PRINT"):
            copies = 1
            rest = text[5:].strip().split(",")[0].strip()
            if rest.isdigit():
                copies = max(1, int(rest))
            self._finalize(copies=copies)
        # GAP / DIRECTION / CLS / DENSITY / SPEED / REFERENCE / CODEPAGE /
        # OFFSET / SHIFT … — accepted and ignored for the preview.

    def _finalize(self, *, copies: int) -> None:
        if self._current is None:
            return
        img = self._current
        self._current = None
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png = buf.getvalue()
        # One preview per label regardless of `copies` — the viewer note
        # carries the copy count via the console.
        self._on_label(png, img.width, img.height)


def _parse_size(text: str) -> Optional[tuple[int, int]]:
    """`SIZE 40 mm, 30 mm` → (40, 30). Best-effort; None on anything odd."""
    try:
        body = text[4:].replace("mm", " ").replace("MM", " ")
        w_str, h_str = body.split(",")
        return int(round(float(w_str.strip()))), int(round(float(h_str.strip())))
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# asyncio TCP server (raw TSPL sink, RAW/9100) — mirrors escpos_printer
# ---------------------------------------------------------------------------


async def _serve_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    state: PrinterState,
    default_width: int,
) -> None:
    peer = writer.get_extra_info("peername")

    def _on_label(png: bytes, w: int, h: int) -> None:
        state.add(png, w, h)

    interp = TsplInterpreter(on_label=_on_label, default_width=default_width)
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
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    log.debug("connection from %s closed", peer)


async def run_server(
    sock, state: PrinterState, default_width: int
) -> None:
    server = await asyncio.start_server(
        lambda r, w: _serve_client(r, w, state, default_width), sock=sock
    )
    async with server:
        await server.serve_forever()


def start_server_thread(
    host: str, port: int, state: PrinterState, default_width: int
) -> int:
    """Bind the RAW sink (with port fallback via ``bind_raw_socket``) and run
    the TSPL server on its own asyncio loop in a daemon thread. Returns the
    ACTUAL bound port — may differ from ``port`` when 9100 was already taken
    (e.g. the receipt emulator is already running alongside)."""
    sock = bind_raw_socket(host, port)
    actual_port = sock.getsockname()[1]

    def _run() -> None:
        asyncio.run(run_server(sock, state, default_width))

    thread = threading.Thread(target=_run, name="label-emulator-server", daemon=True)
    thread.start()
    return actual_port
