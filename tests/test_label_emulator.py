"""TSPL label emulator vs. the manager's real TSPL encoder.

The manager builds label jobs with `src.services.tspl_render.image_to_tspl_bitmap`.
We feed that exact byte stream into the emulator's `TsplInterpreter` and
assert it reconstructs the label pixel-for-pixel — the same lock-step
guarantee the terminal-emulator roundtrip gives.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from emulator.tspl_printer import TsplInterpreter
from src.services.tspl_render import image_to_tspl_bitmap


def _sample_label(width: int = 320, height: int = 240) -> Image.Image:
    img = Image.new("1", (width, height), 1)  # white
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, width - 10, height - 10], outline=0, width=3)
    d.text((30, 40), "PET-42", fill=0)
    d.rectangle([30, 120, 200, 180], fill=0)  # a solid block
    return img


def _collect(interp_feed_chunks: list[bytes]) -> list[tuple[bytes, int, int]]:
    out: list[tuple[bytes, int, int]] = []
    interp = TsplInterpreter(on_label=lambda png, w, h: out.append((png, w, h)))
    for chunk in interp_feed_chunks:
        interp.feed(chunk)
    interp.close()
    return out


def test_reconstructs_label_pixel_for_pixel():
    img = _sample_label()
    blob = image_to_tspl_bitmap(img, label_width_mm=40, label_height_mm=30, gap_mm=2)

    labels = _collect([blob])
    assert len(labels) == 1
    png, w, h = labels[0]
    assert (w, h) == img.size
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    # Decode the emulator's PNG and compare to the original 1-bit image.
    got = Image.open(io.BytesIO(png)).convert("1")
    assert got.size == img.size
    assert got.tobytes() == img.convert("1").tobytes()


def test_survives_chunked_delivery_mid_binary():
    img = _sample_label(160, 120)
    blob = image_to_tspl_bitmap(img, label_width_mm=40, label_height_mm=30, gap_mm=2)
    # Split somewhere inside the BITMAP binary payload.
    mid = blob.index(b"BITMAP") + 40
    labels = _collect([blob[:mid], blob[mid:]])
    assert len(labels) == 1
    assert labels[0][1:] == (160, 120)


def test_multiple_labels_one_connection():
    img = _sample_label(80, 80)
    blob = image_to_tspl_bitmap(img, label_width_mm=40, label_height_mm=30, gap_mm=2)
    labels = _collect([blob + blob + blob])
    assert len(labels) == 3


def test_answers_dle_eot_status_query():
    """The manager's check_status() sends DLE EOT before a job; we must
    reply 0x12 so is_online() passes (same as the receipt emulator)."""
    interp = TsplInterpreter(on_label=lambda *a: None)
    assert interp.feed(b"\x10\x04\x01") == b"\x12"
    # And a status query glued to the front of a real job still works.
    img = _sample_label(80, 80)
    blob = image_to_tspl_bitmap(img, label_width_mm=40, label_height_mm=30, gap_mm=2)
    out: list = []
    interp2 = TsplInterpreter(on_label=lambda png, w, h: out.append((w, h)))
    resp = interp2.feed(b"\x10\x04\x01" + blob)
    assert resp == b"\x12"
    assert out == [(80, 80)]
