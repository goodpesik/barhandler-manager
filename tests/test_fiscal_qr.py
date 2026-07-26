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


def test_qr_readable_and_fits_on_both_widths():
    for url in URLS:
        m = _modules(url)
        for paper in (PAPER_58, PAPER_80):
            box = _qr_box_size(m, paper)
            assert box * m <= paper, (
                f"QR overflows {paper}px paper ({box}*{m}) — would need a "
                f"module-misaligning resize"
            )
            assert box >= 4, (
                f"QR only {box} dots/module on {paper}px paper for {url!r} — "
                f"too small to scan on a thermal head"
            )


def test_box_size_caps_and_floors():
    # Very few modules → capped at 8 (no giant QR).
    assert _qr_box_size(10, PAPER_58) == 8
    # Very dense → shrinks to still fit, never divides by zero.
    assert _qr_box_size(400, PAPER_58) * 400 <= PAPER_58 or _qr_box_size(400, PAPER_58) == 1
    assert _qr_box_size(0, PAPER_58) >= 1
