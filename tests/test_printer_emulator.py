"""ESC/POS printer-emulator contract.

The emulator reconstructs exactly what the manager's bitmap pipeline puts
on the wire (`GS v 0` rasters + a `GS V` cut) and answers the pre-flight
real-time status queries. These tests drive the interpreter with bytes
generated the same way `src/devices/printer.py` generates them.
"""

from __future__ import annotations

import io

from PIL import Image
from escpos.constants import ESC, PAPER_FULL_CUT, RT_STATUS_ONLINE, RT_STATUS_PAPER

from emulator.escpos_printer import EscPosInterpreter, _STATUS_OK
from src.services.bitmap_render import dots_for, image_to_gs_v_0, render_paragraph


def _collector():
    out: list[tuple[bytes, int, int]] = []
    return out, (lambda png, w, h: out.append((png, w, h)))


def _raster_line(text: str, width: int, **kw) -> bytes:
    # Exactly what the bitmap patch emits per line: a GS v 0 + newline.
    return image_to_gs_v_0(render_paragraph(text, width_px=width, **kw)) + b"\n"


def test_status_queries_get_online_paper_ok() -> None:
    """`is_online()` returns False on no reply, so the manager refuses to
    print. We must answer both DLE EOT queries with the ok byte."""
    out, cb = _collector()
    interp = EscPosInterpreter(on_receipt=cb)

    assert interp.feed(RT_STATUS_ONLINE) == _STATUS_OK
    assert interp.feed(RT_STATUS_PAPER) == _STATUS_OK
    # escpos decodes 0x12 as online (RT_MASK_ONLINE 0x08 clear) + paper ok.
    assert not (_STATUS_OK[0] & 0x08)
    assert (_STATUS_OK[0] & 0x12) == 0x12 and (_STATUS_OK[0] & 0x72) != 0x72


def test_status_only_connection_emits_no_receipt() -> None:
    """A manager liveness probe sends a status query and disconnects with
    no print data — it must not spawn a blank receipt."""
    out, cb = _collector()
    interp = EscPosInterpreter(on_receipt=cb)
    interp.feed(RT_STATUS_ONLINE)
    interp.close()
    assert out == []


def test_full_receipt_round_trip_to_png() -> None:
    width = dots_for(58)
    out, cb = _collector()
    interp = EscPosInterpreter(on_receipt=cb)

    wire = (
        _raster_line("ФОП ЛЕВИНЕЦЬ", width, bold=True, align="center")
        + _raster_line("Готівка    100.00 ГРН", width)
        + ESC + b"d" + bytes([6])       # print_and_feed(6)
        + PAPER_FULL_CUT                 # GS V 0
    )
    interp.feed(wire)

    assert len(out) == 1
    png, w, h = out[0]
    assert w == 384
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png))
    assert img.size == (384, h)
    # Reconstructed content must contain black pixels (not a blank roll).
    assert img.convert("L").getextrema()[0] == 0


def test_partial_command_buffering() -> None:
    """TCP delivers arbitrary chunk boundaries — a command split across two
    feeds must still parse, never desync."""
    width = dots_for(58)
    out, cb = _collector()
    interp = EscPosInterpreter(on_receipt=cb)
    wire = _raster_line("split me down the middle", width) + PAPER_FULL_CUT

    for cut_at in (1, 5, 9, len(wire) // 2, len(wire) - 1):
        out.clear()
        interp = EscPosInterpreter(on_receipt=cb)
        interp.feed(wire[:cut_at])
        interp.feed(wire[cut_at:])
        assert len(out) == 1, f"split at {cut_at} produced {len(out)} receipts"


def test_paper_width_auto_detected_from_raster() -> None:
    out, cb = _collector()
    interp = EscPosInterpreter(on_receipt=cb, default_width=384)
    interp.feed(_raster_line("80mm kitchen ticket", dots_for(80)) + PAPER_FULL_CUT)
    assert out[0][1] == 576           # 80mm, even though default was 58mm


def test_bitmap_lines_have_no_blank_gaps() -> None:
    """Regression: every bitmap line is `GS v 0` + `\\n`. The trailing LF is
    the raster's terminator and must NOT add a blank line — otherwise each
    line gets ~2x its height and the receipt looks loosely spaced (unlike a
    real printer). N stacked rasters must equal N strip heights + margins."""
    width = dots_for(58)
    out, cb = _collector()
    interp = EscPosInterpreter(on_receipt=cb)

    strip = render_paragraph("line", width_px=width)
    n = 6
    wire = (image_to_gs_v_0(strip) + b"\n") * n + PAPER_FULL_CUT
    interp.feed(wire)

    _, _, h = out[0]
    margin = 16 * 2
    assert h == strip.height * n + margin, (
        f"expected {strip.height * n + margin}, got {h} — blank lines leaked in"
    )


def test_native_text_mode_decodes_codepage() -> None:
    """`native` render mode sends ESC t <table> + raw code-page bytes. We
    decode them for the preview instead of producing a raster."""
    out, cb = _collector()
    interp = EscPosInterpreter(on_receipt=cb, default_width=384)
    interp.feed(
        b"\x1bt\x11"                  # ESC t 17 → cp866
        + "Дякуємо".encode("cp866")
        + b"\n"
        + PAPER_FULL_CUT
    )
    assert len(out) == 1
    png, w, h = out[0]
    assert w == 384 and png[:4] == b"\x89PNG"
