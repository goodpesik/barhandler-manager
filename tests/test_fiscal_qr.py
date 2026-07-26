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
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
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
            assert box >= 4, (
                f"QR only {box} dots/module on {paper}px for {url!r} — "
                f"too small to scan on a thermal head"
            )
        # Fixed physical size: same on 58 & 80 (NOT full-width on 80mm),
        # and ~3 cm rather than a giant QR eating the wide receipt.
        assert b58 == b80, f"QR size differs across widths ({b58} vs {b80})"
        assert b80 * m <= 320, f"QR too big on 80mm: {b80 * m}px"


def test_box_size_floor_and_guard():
    # Dense QR: floored at 4 dots/module (readable) and still fits.
    assert _qr_box_size(70, PAPER_58) >= 4
    assert _qr_box_size(70, PAPER_58) * 70 <= PAPER_58
    # A short QR stays ~target width, not the full paper.
    assert _qr_box_size(25, PAPER_80) * 25 <= 320
    # Divide-by-zero guard.
    assert _qr_box_size(0, PAPER_58) >= 1
