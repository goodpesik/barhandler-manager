"""Render a structured receipt payload into ESC/POS commands.

The web app gives us a typed payload (header / items / total / footer);
the renderer executes the ESC/POS calls against a python-escpos printer
instance. Layout is paper-width-aware — 32 chars/line for 58mm,
48 chars/line for 80mm.
"""

from __future__ import annotations

from typing import Iterable

from src.models.receipt import ReceiptItem, ReceiptPayload


PAYMENT_LABELS = {
    "cash": "Готівка",
    "card": "Картка",
    "acquiring": "Еквайринг",
}


def _format_price(value: float) -> str:
    # 1234.5 → "1234.50 ГРН"; trims to two decimals.
    return f"{value:.2f} ГРН"


def _two_column(left: str, right: str, width: int) -> str:
    """Pack a row into one line, truncating left side if needed."""
    right_len = len(right)
    left_budget = max(0, width - right_len - 1)
    truncated = left if len(left) <= left_budget else left[: max(left_budget - 1, 0)] + "…"
    return f"{truncated:<{left_budget}} {right:>{right_len}}"


def _item_line(item: ReceiptItem, width: int) -> Iterable[str]:
    """Render one item; long names wrap to a second line under the qty x price."""
    right = f"{item.qty} x {_format_price(item.price)}"
    primary = _two_column(item.name, right, width)
    yield primary
    # Per-item subtotal underneath for clarity
    subtotal = _format_price(item.qty * item.price)
    yield _two_column("", subtotal, width)


def render_receipt(printer, payload: ReceiptPayload, *, chars_per_line: int) -> None:
    """Issue ESC/POS commands to `printer` for `payload`.

    Pure side-effect — call this from inside a PrinterDevice.enqueue() job.
    """
    width = chars_per_line

    if payload.header:
        printer.set(align="center", bold=True)
        printer.text(payload.header + "\n")
        printer.set(align="left", bold=False)
        printer.text("-" * width + "\n")

    for item in payload.items:
        for line in _item_line(item, width):
            printer.text(line + "\n")

    printer.text("-" * width + "\n")
    printer.set(bold=True)
    printer.text(_two_column("ВСЬОГО:", _format_price(payload.total), width) + "\n")
    printer.set(bold=False)

    payment_label = PAYMENT_LABELS.get(payload.payment.lower(), payload.payment)
    printer.text(_two_column("Оплата:", payment_label, width) + "\n")

    if payload.footer:
        printer.text("\n")
        printer.set(align="center")
        printer.text(payload.footer + "\n")
        printer.set(align="left")

    printer.text("\n\n")
    printer.cut()
