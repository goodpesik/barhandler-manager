"""Fiscal-receipt QR must stay scannable on 58 mm thermal paper.

The manager (not the frontend) renders the QR from `qr_url`. The old code
targeted a fixed ~160-dot QR, which on longer fiscal URLs left only 2-3 dots
per module — cheap 58 mm heads smear that into an unscannable blob. The fix
fills the paper width with the largest integer dots-per-module; these tests
pin that it stays readable (>=4 dots/module) and never overflows the paper
(no distorting resize).
"""

from __future__ import annotations

import qrcode

from src.services.fiscal_receipt import _qr_box_size

PAPER_58 = 384  # dots
PAPER_80 = 576

# Realistic fiscal check URLs: short (Vchasno) and long (ДПС cabinet w/ params).
URLS = [
    "https://kasa.vchasno.com.ua/check/TEST_e5rHVICc6weYAQ",
    "https://cabinet.tax.gov.ua/cashregs/check?id=99999999&fn=TEST_e5rHVICc6weYAQ"
    "&date=20260421&time=091542&sm=263.00",
]


def _modules(url: str) -> int:
    # border=4 mirrors the ISO quiet zone the renderer now uses.
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    return qr.modules_count + qr.border * 2


def test_qr_readable_fits_and_consistent_across_widths():
    for url in URLS:
        m = _modules(url)
        b58 = _qr_box_size(m, PAPER_58)
        b80 = _qr_box_size(m, PAPER_80)
        for paper, box in ((PAPER_58, b58), (PAPER_80, b80)):
            assert box * m <= paper, f"QR overflows {paper}px paper ({box}*{m})"
            # Ink-spread floor: typical fiscal URLs (that fit ≥7 dots/module)
            # must print big modules so the 1-dot erosion + thermal bleed still
            # scans. Only a QR too dense to fit 7 dots may fall back lower.
            if PAPER_58 // m >= 7:
                assert box >= 7, (
                    f"QR only {box} dots/module on {paper}px for {url!r} — "
                    f"too small to survive ink-spread on a cheap thermal head"
                )
        # Same physical size on 58 & 80 mm — never full-width on 80 mm (the
        # user's "80 на всю ширину — зашквар"): well under the 80 mm width.
        assert b58 == b80, f"QR size differs across widths ({b58} vs {b80})"
        assert b80 * m <= 0.75 * PAPER_80, f"QR too wide on 80mm: {b80 * m}px"


def test_box_size_floor_and_guard():
    # Bigger modules for ink-spread tolerance: a typical 41-module fiscal QR
    # gets ~8 dots/module (≈4 cm), the same on 58 and 80 mm.
    assert _qr_box_size(41, PAPER_58) >= 7
    assert _qr_box_size(41, PAPER_58) == _qr_box_size(41, PAPER_80)
    # Dense QR that can't fit the floor: capped by the paper, still fits.
    assert _qr_box_size(70, PAPER_58) * 70 <= PAPER_58
    # A short QR stays a sensible size, not the full 80 mm width.
    assert _qr_box_size(25, PAPER_80) * 25 <= 0.75 * PAPER_80
    # Divide-by-zero guard.
    assert _qr_box_size(0, PAPER_58) >= 1
