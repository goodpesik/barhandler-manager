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
PAPER_DOTS = {58: 384, 80: 576}

# Base font size in pixels for "normal" text. Tuned so Noto Sans Mono
# fits exactly 32 chars across 58mm (384 dots) and 48 chars across 80mm
# (576 dots) — at this size the advance width sits at ~12px per glyph.
BASE_FONT_PX = 20

Align = Literal["left", "center", "right"]


def dots_for(paper_width_mm: int) -> int:
    return PAPER_DOTS.get(paper_width_mm, PAPER_DOTS[58])


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

    Empty `text` still yields an image (a blank line at the current font
    height) so the caller can preserve line spacing.
    """
    # `scale_width` stretches horizontally only — we render at base width
    # and resize the final bitmap after for ESC/POS double-width semantics.
    font_scale = scale_height
    font = _font(bold=bold, scale=font_scale)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + padding_y * 2

    if not text:
        return Image.new("1", (width_px, line_h), 1)

    # Render onto an oversized canvas to measure, then crop to width_px.
    text_w, _ = measure(text, bold=bold, scale=font_scale)
    canvas = Image.new("1", (max(width_px, text_w + 4), line_h), 1)
    draw = ImageDraw.Draw(canvas)

    if align == "right":
        x = max(0, width_px - text_w)
    elif align == "center":
        x = max(0, (width_px - text_w) // 2)
    else:
        x = 0
    draw.text((x, padding_y), text, font=font, fill=0)

    # Hard-clip to printer's pixel width — long lines get truncated.
    out = canvas.crop((0, 0, width_px, line_h))
    if scale_width != 1.0:
        new_w = width_px
        out = out.resize((new_w, line_h))  # placeholder: ESC/POS handles double-width via state
    return out


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
