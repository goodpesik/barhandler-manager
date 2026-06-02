"""TSPL (Tag Setup Programming Language) bitmap renderer.

TSPL is the wire protocol most dedicated thermal label printers use
out-of-the-box: TSC, Xprinter XP-235B/237B/246B, GoDEX, Argox. These
printers ship in "LABEL" mode (per the self-test) and silently drop
ESC/POS commands — `GS v 0 ...` writes succeed at the USB layer but
the firmware ignores them, which is why our previous `/print/label`
implementation looked OK in logs (200 OK) but nothing physically
printed.

This module renders a PIL image to a TSPL command sequence:

  SIZE <width_mm> mm, <height_mm> mm
  GAP  <gap_mm> mm, 0 mm
  DIRECTION 0
  CLS
  BITMAP 0,0,<bytes_per_row>,<height_px>,0,<binary_data>
  PRINT 1

Notes:
- BITMAP mode 0 = OVERWRITE (matches the image as-is, no inversion).
- Binary data is MSB-first, 1 = black, 0 = white — same packing as
  PIL's "1" mode AFTER inversion (PIL packs 1 = white).
- Numeric values in the text header are ASCII; the binary `data` is
  appended verbatim after the comma.
- Lines terminated with CRLF (TSPL requires it; LF-only works on most
  Xprinter firmware but the spec says CRLF).
"""

from PIL import Image


def image_to_tspl_bitmap(
    img: Image.Image,
    *,
    label_width_mm: int,
    label_height_mm: int,
    gap_mm: float,
    copies: int = 1,
) -> bytes:
    """Encode a PIL image as a complete TSPL print job.

    `img` is resized externally (caller scales to the printer's
    dot_width); we just pack it. The returned bytes include the SIZE/
    GAP/CLS/BITMAP/PRINT framing — write the whole blob via `_raw()`
    on python-escpos's USB endpoint.
    """
    img = img.convert("1")
    width_px, height_px = img.size
    bytes_per_row = (width_px + 7) // 8

    # PIL "1" mode packs MSB-first with `1 = white, 0 = black` (verified
    # empirically: `Image.new("1", (8,1), 1).tobytes() == b'\xff'`). The
    # TSPL BITMAP spec says bit 1 = black, but XP-246B/235B ship with
    # "DIRECTION MODE: INVERSE" enabled (visible on the self-test
    # printout) which flips bit semantics again. End-to-end the PIL
    # bytes pass through verbatim — no XOR. Adding our own inversion
    # was the bug that printed nearly-white labels as solid black.
    raw = img.tobytes()

    # DIRECTION 1 matches the factory "DIRECTION MODE: INVERSE" line on
    # Xprinter self-tests — the printer's mechanical feed direction
    # is "labels exit head-first into the operator's hand", so the
    # image needs to print bottom-up to come out reading top-to-bottom.
    header = (
        f"SIZE {label_width_mm} mm, {label_height_mm} mm\r\n"
        f"GAP {gap_mm:g} mm, 0 mm\r\n"
        "DIRECTION 1\r\n"
        "CLS\r\n"
        f"BITMAP 0,0,{bytes_per_row},{height_px},0,"
    ).encode("ascii")
    footer = ("\r\n" f"PRINT {copies}\r\n").encode("ascii")
    return header + raw + footer
