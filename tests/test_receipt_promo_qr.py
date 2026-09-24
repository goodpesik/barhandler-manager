"""PET-921 — власний QR закладу на чеку продажу.

Папір — єдине, що людина забирає з собою, тож заклад друкує на ньому, куди
прийти далі: телеграм-бот розсилок, інстаграм, сайт. Власник поставив дві
умови: код стоїть ПЕРЕД брендами («перед petshandler та назвою фіскального
оператора») і виходить ТИМ САМИМ квадратом, що й податковий.

Друга умова — не про красу: розмір коду росте від довжини вмісту, і якби ми
малювали промо-QR окремим кодом, короткий вміст дав би інший квадрат, а довгий
— дрібніші модулі, яких дешева 58-мм голова не зчитує.
"""

from __future__ import annotations

from datetime import datetime

from PIL import Image

from src.models.fiscal_receipt import FiscalReceipt, FiscalReceiptItem
from src.services.fiscal_receipt import render_fiscal_receipt

TAX_URL = (
    "https://cabinet.tax.gov.ua/cashregs/check?date=20260922&time=203711"
    "&fn=4001090155&id=8b1f2c44-9d3e-4b77-9a10-5f2c3d7e1a09&sm=260.95"
)
BOT_URL = "https://t.me/petshandler_notify_bot?start=a3f91c"


class FakePrinter:
    """Записує і рядки, і картинки — QR приїжджає саме картинкою."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def text(self, value: str) -> None:
        self.calls.append(("text", value))

    def set(self, **_kw) -> None:
        pass

    def cut(self, **_kw) -> None:
        pass

    def _raw(self, *_a, **_kw) -> None:  # pragma: no cover — є _bh_emit_image
        pass

    def _bh_emit_image(self, image: Image.Image) -> None:
        self.calls.append(("image", image))

    # --- зручні витяги ---
    @property
    def images(self) -> list[Image.Image]:
        return [v for kind, v in self.calls if kind == "image"]

    @property
    def printed(self) -> str:
        return "".join(str(v) for kind, v in self.calls if kind == "text")

    def index_of_text(self, needle: str) -> int:
        for i, (kind, v) in enumerate(self.calls):
            if kind == "text" and needle in str(v):
                return i
        raise AssertionError(f"на чеку немає {needle!r}: {self.printed!r}")

    def index_of_image(self, which: int) -> int:
        seen = -1
        for i, (kind, _v) in enumerate(self.calls):
            if kind == "image":
                seen += 1
                if seen == which:
                    return i
        raise AssertionError(f"на чеку немає картинки №{which}")


def _receipt(**over) -> FiscalReceipt:
    base = dict(
        business_name="ФОП Грекова Оксана Володимирівна",
        items=[FiscalReceiptItem(name="Корм", quantity=1, price=260.95, sum=260.95)],
        payment_name="Картка",
        paid_sum=260.95,
        total_sum=260.95,
        fiscal_date=datetime(2026, 9, 23, 18, 30),
        qr_url=TAX_URL,
        operator="checkbox\npetshandler",
    )
    base.update(over)
    return FiscalReceipt(**base)


def _render(receipt: FiscalReceipt, chars: int = 48) -> FakePrinter:
    printer = FakePrinter()
    render_fiscal_receipt(printer, receipt, chars_per_line=chars)
    return printer


def test_no_promo_qr_when_the_shop_did_not_ask_for_one():
    printer = _render(_receipt())

    # Рівно один код — податковий.
    assert len(printer.images) == 1


def test_promo_qr_is_printed_with_its_caption():
    printer = _render(
        _receipt(promo_qr=BOT_URL, promo_qr_caption="Підписатися на новини")
    )

    assert len(printer.images) == 2
    assert "Підписатися на новини" in printer.printed


def test_promo_qr_stands_before_the_brands_and_after_the_tax_one():
    printer = _render(
        _receipt(promo_qr=BOT_URL, promo_qr_caption="Підписатися на новини")
    )

    tax_qr = printer.index_of_image(0)
    caption = printer.index_of_text("Підписатися на новини")
    promo_qr = printer.index_of_image(1)
    brands = printer.index_of_text("PETSHANDLER")

    # Підпис над своїм кодом, сам код — після податкового й перед брендами.
    assert tax_qr < caption < promo_qr < brands


def test_both_codes_come_out_the_same_square():
    """Головна вимога власника: «квадрат такий самий як податковий».

    Вміст у них різний (податковий URL — 131 символ, посилання на бот — 48),
    тож модулів різна кількість. Однаковими їх тримає спільна функція розміру:
    коротший вміст друкується товщими модулями, а не меншим кодом.
    """
    for chars, tolerance in ((48, 0.1), (32, 0.1)):  # 80 мм і 58 мм
        printer = _render(_receipt(promo_qr=BOT_URL), chars=chars)
        tax, promo = printer.images

        assert abs(tax.width - promo.width) <= tolerance * tax.width, (
            f"{chars} симв/рядок: податковий {tax.width}px, промо {promo.width}px"
        )


def test_caption_is_optional():
    printer = _render(_receipt(promo_qr=BOT_URL))

    assert len(printer.images) == 2
