"""Друкований чек показує КОЖНУ частину оплати.

Власник закрив чек на 300 двома способами — 200 готівкою і 100 карткою. На
екрані так і стояло, а з принтера вийшло «Готівка 300.00»: модель чека мала
рівно одне поле способу й одну суму, тож друкувався основний спосіб і ВЕСЬ
підсумок. Людина з паперу бачила, що всі 300 взяли готівкою.
"""

from __future__ import annotations

from datetime import datetime

from src.models.fiscal_receipt import FiscalReceipt, FiscalReceiptItem
from src.services.fiscal_receipt import render_fiscal_receipt


class FakePrinter:
    """Приймає те саме, що й справжній: нас цікавлять надруковані рядки."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def text(self, value: str) -> None:
        self.lines.append(value)

    def set(self, **_kw) -> None:
        pass

    def image(self, *_a, **_kw) -> None:  # pragma: no cover — QR тут не друкуємо
        pass

    def cut(self, **_kw) -> None:
        pass

    def _raw(self, *_a, **_kw) -> None:
        pass


def _receipt(**over) -> FiscalReceipt:
    base = dict(
        business_name="ФОП Грекова Оксана Володимирівна",
        items=[FiscalReceiptItem(name="назва 4", quantity=1, price=300.0, sum=300.0)],
        payment_name="Готівка",
        paid_sum=300.0,
        total_sum=300.0,
        fiscal_date=datetime(2026, 9, 21, 17, 24),
    )
    base.update(over)
    return FiscalReceipt(**base)


def _render(receipt: FiscalReceipt) -> str:
    printer = FakePrinter()
    render_fiscal_receipt(printer, receipt, chars_per_line=32)
    return "".join(printer.lines)


def test_each_part_is_printed_with_its_own_sum():
    out = _render(
        _receipt(
            payment_name="Готівка",
            paid_sum=200.0,
            payments=[
                {"name": "Готівка", "sum": 200.0},
                {"name": "Картка", "sum": 100.0},
            ],
        )
    )
    assert "Готівка" in out and "200.00 грн" in out
    assert "Картка" in out and "100.00 грн" in out
    # Саме те, що бачив власник: уся сума під одним способом.
    assert "300.00 грн" not in out.split("Вид операції")[1].split("СУМА")[0], (
        "у рядку оплати знову стоїть увесь підсумок замість частини"
    )


def test_the_parts_add_up_to_the_receipt_total():
    out = _render(
        _receipt(
            paid_sum=200.0,
            payments=[
                {"name": "Готівка", "sum": 200.0},
                {"name": "Картка", "sum": 100.0},
            ],
        )
    )
    assert "СУМА" in out
    assert "300.00" in out.split("СУМА")[1]


def test_a_plain_receipt_still_prints_one_line():
    """Звичайний чек одним способом друкується як і доти."""
    out = _render(_receipt())
    payment_block = out.split("Вид операції")[1].split("СУМА")[0]
    assert "Готівка" in payment_block
    assert "300.00 грн" in payment_block


def test_the_card_part_keeps_the_acquiring_block():
    """Дані терміналу друкуються після рядків оплати — і з розбивкою теж."""
    out = _render(
        _receipt(
            paid_sum=200.0,
            payments=[
                {"name": "Готівка", "sum": 200.0},
                {"name": "Картка", "sum": 100.0},
            ],
            acquirer={"cardmask": "************1234", "rrn": "161240165707"},
        )
    )
    assert "161240165707" in out
    assert out.index("100.00 грн") < out.index("161240165707")
