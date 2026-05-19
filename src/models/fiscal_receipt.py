"""Unified fiscal-receipt model used by POST /print/fiscal.

Both Checkbox and Vchasno Kasa return structured JSON receipts. BarHandler's
mapper (planned — see docs/INTEGRATION-SPEC.md) flattens them into this model
before sending to the printer, so the manager renders ONE consistent layout
regardless of which fiscal operator produced the receipt.

Numbers stay numbers (no pre-formatted strings) so the renderer can align
columns properly on 58mm/80mm paper and emphasise the СУМА row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class FiscalReceiptItem(BaseModel):
    name: str
    quantity: float = Field(gt=0)
    price: float = Field(ge=0)        # за одиницю
    sum: float = Field(ge=0)          # підсумок (quantity * price з заокругленням операторa)
    tax_symbol: Optional[str] = None  # "Ж", "А", "Б"... — друкується праворуч
    uktzed: Optional[str] = None      # код УКТЗЕД
    barcode: Optional[str] = None
    excise_codes: list[str] = Field(default_factory=list)


class FiscalReceiptTax(BaseModel):
    name: str          # "Без ПДВ + акциз" / "ПДВ" / "Акцизний податок"
    symbol: str = ""   # "Ж" / "А" / ""
    rate: float = 0    # %, 0..100
    value: float = 0   # сума податку в чеку


class AcquirerInfo(BaseModel):
    """Optional acquirer block — present on card transactions only."""

    bank_id: Optional[str] = None
    bank_name: Optional[str] = None
    terminal_id: Optional[str] = None
    cardmask: Optional[str] = None
    rrn: Optional[str] = None
    auth_code: Optional[str] = None
    paysys: Optional[str] = None         # "Visa" / "MasterCard"
    commission: Optional[float] = None
    operation: Optional[str] = None      # "Оплата" / "Повернення"
    method_name: Optional[str] = None    # "Чіп" / "PayPass"


class FiscalReceipt(BaseModel):
    # ---- Header ----
    receipt_type: str = "ФІСКАЛЬНИЙ ЧЕК"   # "ТЕСТОВИЙ ЧЕК" for test mode
    business_name: str = ""                # "ФОП ЛЕВИНЕЦЬ МАКСИМ СЕРГІЙОВИЧ"
    point_name: Optional[str] = None       # "Тестова торгова точка"
    address: Optional[str] = None          # "Україна, м.Київ, вул. Хрещатик, 99"
    tax_id: Optional[str] = None           # "ІД 3179210933" / EDRPOU
    establishment: Optional[str] = None    # custom venue header (receipt_header from settings)

    # ---- Items ----
    items: list[FiscalReceiptItem] = Field(min_length=1)

    # ---- Payment ----
    payment_name: str = "Готівка"          # "Готівка" / "Картка" / "Безготівкова"
    operation: str = "Оплата"              # "Оплата" / "Повернення"
    paid_sum: float = Field(ge=0)          # фактично сплачено
    total_sum: float = Field(ge=0)         # до сплати (зазвичай == paid_sum)

    # ---- Taxes ----
    taxes: list[FiscalReceiptTax] = Field(default_factory=list)

    # ---- Acquirer (optional) ----
    acquirer: Optional[AcquirerInfo] = None

    # ---- Fiscal block ----
    fiscal_number: str = ""                # "TEST_e5rHVICc6weYAQ" — фіскальний номер чека
    fiscal_date: datetime
    pos_fiscal_number: Optional[str] = None  # "ФН ПРРО 9999993179210933"
    cashier: Optional[str] = None
    online_mode: bool = True                 # "Режим роботи: Онлайн" / "Офлайн"

    # ---- Footer ----
    comment: Optional[str] = None            # "Коментар: ..."
    footer: Optional[str] = None             # "ДЯКУЄМО ЗА ПОКУПКУ"

    # ---- QR ----
    qr_url: Optional[str] = None             # URL для перевірки чека (з фіск. оператора)

    # ---- Operator brand ----
    operator: Optional[str] = None           # "checkbox" / "vchasno_kasa" / None

    # ---- Print options ----
    open_drawer: bool = False
    is_refund: bool = False                  # друкує "ПОВЕРНЕННЯ" замість стандартної шапки
