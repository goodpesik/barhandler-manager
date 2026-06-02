"""Render text → 1-bit bitmap → ESC/POS raster image.

Bitmap mode is the default rendering pipeline for receipts because it
guarantees correct Unicode (cyrillic, ї/і/ґ, emojis, anything) on any
ESC/POS printer regardless of which code pages its firmware exposes.
We carry our own TTF font so the typography is consistent across
printers — DejaVu Sans Mono ships full Cyrillic glyph coverage.

The tradeoff vs native code pages is wire size: a 60-line receipt prints
~40 KB of raster instead of ~2 KB of plain text. That's perfectly fine
on USB and fast enough for normal POS use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FONT_REGULAR = ASSETS_DIR / "NotoSansMono-Regular.ttf"
FONT_BOLD = ASSETS_DIR / "NotoSansMono-Bold.ttf"

# Dot widths of common thermal printer paper sizes.
# 48mm label (XP-246B): 8 dots/mm × 48mm = 384 printable dots (same pitch as 58mm).
# 40mm label: 8 dots/mm × 40mm = 320 dots — common Xprinter label stock.
PAPER_DOTS = {40: 320, 48: 384, 58: 384, 80: 576}

# Base font size in pixels for "normal" text. Tuned so Noto Sans Mono
# fits exactly 32 chars across 58mm (384 dots) and 48 chars across 80mm
# (576 dots) — at this size the advance width sits at ~12px per glyph.
BASE_FONT_PX = 20

Align = Literal["left", "center", "right"]


def dots_for(paper_width_mm: int) -> int:
    # Known sizes win; otherwise compute from the 8 dots/mm pitch every
    # thermal head we've seen uses, rounded to the byte boundary.
    if paper_width_mm in PAPER_DOTS:
        return PAPER_DOTS[paper_width_mm]
    return max(8, (paper_width_mm * 8 // 8) * 8)


def _font(*, bold: bool, scale: float) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    size = max(8, int(BASE_FONT_PX * scale))
    return ImageFont.truetype(str(path), size)


def measure(text: str, *, bold: bool = False, scale: float = 1.0) -> tuple[int, int]:
    """Pixel size that `text` would occupy with the given style."""
    font = _font(bold=bold, scale=scale)
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def render_line(
    text: str,
    *,
    width_px: int,
    bold: bool = False,
    align: Align = "left",
    scale_height: float = 1.0,
    scale_width: float = 1.0,
    padding_y: int = 2,
) -> Image.Image:
    """Render a single line of text as a 1-bit B&W image `width_px` wide.

    The text is always rasterised at `BASE_FONT_PX` so the glyph advance
    is constant — this matches ESC/POS semantics where `double_height`
    multiplies pixel height but leaves the column advance unchanged, and
    `double_width` is the column-advance multiplier. We then stretch the
    final bitmap with PIL.resize() to apply the requested scale.

    Empty `text` still yields an image (a blank line at the current font
    height) so the caller can preserve line spacing.
    """
    font = _font(bold=bold, scale=1.0)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + padding_y * 2

    if not text:
        canvas = Image.new("1", (width_px, line_h), 1)
    else:
        text_w, _ = measure(text, bold=bold, scale=1.0)
        canvas = Image.new("1", (max(width_px, text_w + 4), line_h), 1)
        draw = ImageDraw.Draw(canvas)
        if align == "right":
            x = max(0, width_px - text_w)
        elif align == "center":
            x = max(0, (width_px - text_w) // 2)
        else:
            x = 0
        draw.text((x, padding_y), text, font=font, fill=0)
        canvas = canvas.crop((0, 0, width_px, line_h))

    # Apply double-height as a pure vertical stretch — glyphs grow taller
    # but the column advance is unchanged, so a full-width 32-char line
    # still fits exactly on 58mm paper at double-height.
    if scale_height != 1.0:
        canvas = canvas.resize((canvas.width, max(1, int(canvas.height * scale_height))))
    # Double-width: horizontal stretch. The caller is responsible for
    # supplying a short-enough layout because doubling 32 chars overflows
    # the paper width — we crop instead of scaling glyphs sideways.
    if scale_width != 1.0:
        new_w = max(1, int(canvas.width * scale_width))
        canvas = canvas.resize((new_w, canvas.height))
        if canvas.width > width_px:
            canvas = canvas.crop((0, 0, width_px, canvas.height))
    return canvas


def render_paragraph(
    text: str,
    *,
    width_px: int,
    bold: bool = False,
    align: Align = "left",
    scale_height: float = 1.0,
    scale_width: float = 1.0,
) -> Image.Image:
    """Multi-line render. Splits on `\\n`, concatenates vertically."""
    lines = text.split("\n")
    images = [
        render_line(
            line,
            width_px=width_px,
            bold=bold,
            align=align,
            scale_height=scale_height,
            scale_width=scale_width,
        )
        for line in lines
    ]
    total_h = sum(i.height for i in images)
    out = Image.new("1", (width_px, total_h), 1)
    y = 0
    for img in images:
        out.paste(img, (0, y))
        y += img.height
    return out


def image_to_gs_v_0(img: Image.Image) -> bytes:
    """Encode a PIL 1-bit image as raw `GS v 0` raster bit-image command.

    We bypass python-escpos' image() because its profile-driven dispatch
    sometimes picks an `ESC *` column-mode path that the STMicro-class
    printers mis-decode (the raster bytes end up in the text buffer and
    print as ASCII). The `GS v 0` family is the most-widely-supported
    ESC/POS raster command — accepted by virtually every 58/80mm printer.
    """
    img = img.convert("1")
    width, height = img.size
    bytes_per_row = (width + 7) // 8
    header = bytes([
        0x1D, 0x76, 0x30, 0x00,            # GS v 0  (m=0 normal)
        bytes_per_row & 0xFF,
        (bytes_per_row >> 8) & 0xFF,
        height & 0xFF,
        (height >> 8) & 0xFF,
    ])
    # PIL '1' mode: 0 = black, 255 = white. ESC/POS wants 1 = black.
    raw = img.tobytes()                     # already MSB-first, padded to byte
    # PIL packs 1-bit images as 1 = white. We need to invert.
    inverted = bytes(b ^ 0xFF for b in raw)
    return header + inverted
