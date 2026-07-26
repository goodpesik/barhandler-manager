"""Render a FiscalReceipt → ESC/POS commands.

Visual reference: docs/samples/vchasno-kasa-test.pdf (a real fiscal receipt
printed by Vchasno Kasa). We try to match that layout as closely as a
58mm thermal printer allows:

  ************************************************
                  ФІСКАЛЬНИЙ ЧЕК                       (bold, double-size)
  ************************************************
              ФОП ЛЕВИНЕЦЬ МАКСИМ                      (bold, centered)
                Тестова торгова точка                  (centered)
            Україна, м.Київ, вул. ...                  (centered)
                  ІД 3179210933                        (centered)
  ------------------------------------------------
                  Тестовий заклад                        (establishment line, centered)
  ------------------------------------------------
  1 x 263.00
  УКТЗЕД 12345
  Бурбон 50                              263.00 Ж
  ------------------------------------------------
  Вид операції                            Оплата
  Картка                              263.00 грн
  ------------------------------------------------
  СУМА                                  263.00         (bold, double-size)
  Без ПДВ + акциз 5% Ж                       0.00
  Акцизний податок 5% Ж                     12.52
  До сплати                          263.00 грн
  ------------------------------------------------
  ЧЕК № TEST_e5...
  21.04.2026 09:15:42
                  [QR code]                            (native ESC/POS)
  ------------------------------------------------
  Режим роботи: Онлайн
  ФН ПРРО 9999993179210933
                ФІСКАЛЬНИЙ ЧЕК                         (bold, centered)
"""

from __future__ import annotations

import qrcode
from PIL import Image

from src.models.fiscal_receipt import FiscalReceipt, FiscalReceiptItem
from src.services.bitmap_render import dots_for, image_to_gs_v_0


def _qr_box_size(modules_across: int, paper_w: int) -> int:
    """Dots per QR module.

    Target a fixed physical size (~3 cm) so the QR looks the SAME sensible
    size on 58 mm and 80 mm — filling the paper width would print a huge QR on
    80 mm. But floor at 4 dots/module: below that a cheap 58 mm thermal head
    smears the modules into an unscannable blob (this was the bug — the old
    ~160-dot target dropped long fiscal URLs to 2-3 dots/module). Never wider
    than the paper, so `box * modules_across <= paper_w` and we never resize
    (resizing a 1-bit QR misaligns modules and breaks scanning)."""
    TARGET_DOTS = 240  # ~3 cm at 8 dots/mm
    m = max(1, modules_across)
    box = max(4, round(TARGET_DOTS / m))   # readable floor
    return max(1, min(box, paper_w // m))  # but never overflow the paper


def _format_money(value: float) -> str:
    return f"{value:.2f}"


def _two_col(left: str, right: str, width: int) -> str:
    right_len = len(right)
    left_budget = max(0, width - right_len - 1)
    if len(left) > left_budget:
        left = left[: max(left_budget - 1, 0)] + "…"
    return f"{left:<{left_budget}} {right:>{right_len}}"


def _wrap_lines(text: str, width: int) -> list[str]:
    """Word-wrap text into rows ≤ `width` columns. Caller handles
    centering via `printer.set(align="center")` — we never pad with
    spaces because the bitmap renderer would then center the padded
    string and the line would drift further right."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _separator(width: int, char: str = "-") -> str:
    return char * width


def _render_item(printer, item: FiscalReceiptItem, width: int) -> None:
    # Quantity / unit-price header line. Matches Vchasno PDF: "1 x 263.00".
    qty = f"{item.quantity:g}"
    printer.text(f"{qty} x {_format_money(item.price)}\n")
    if item.uktzed:
        printer.text(f"УКТЗЕД {item.uktzed}\n")
    if item.barcode:
        printer.text(f"Код {item.barcode}\n")
    if item.excise_codes:
        printer.text(f"Акциз {','.join(item.excise_codes)}\n")

    # Name + price + tax marker right-aligned.
    right = _format_money(item.sum)
    if item.tax_symbol:
        right = f"{right} {item.tax_symbol}"
    printer.text(_two_col(item.name, right, width) + "\n")

    # Per-line discount, like the fiscal receipt: the discount and the
    # discounted line total below the full price. Indented under the item.
    if item.discount and item.discount > 0:
        printer.text(
            _two_col("  Знижка", f"-{_format_money(item.discount)}", width) + "\n"
        )
        printer.text(
            _two_col(
                "  Сума зі знижкою",
                _format_money(item.sum - item.discount),
                width,
            )
            + "\n"
        )


def render_fiscal_receipt(printer, receipt: FiscalReceipt, *, chars_per_line: int) -> None:
    """Drive `printer` (python-escpos instance) to print `receipt`."""
    width = chars_per_line

    # ---- Header banner ----
    printer.set(align="center", bold=True, double_height=False, double_width=False)
    printer.text("*" * width + "\n")
    printer.set(align="center", bold=True, double_height=True, double_width=False)
    printer.text(receipt.receipt_type + "\n")
    printer.set(align="center", bold=True, double_height=False, double_width=False)
    printer.text("*" * width + "\n")
    if receipt.business_name:
        for line in _wrap_lines(receipt.business_name, width):
            printer.text(line + "\n")
    printer.set(align="center", bold=False)
    for text in filter(None, (receipt.point_name, receipt.address, receipt.tax_id)):
        for line in _wrap_lines(text, width):
            printer.text(line + "\n")

    # ---- Establishment (venue header) ----
    # Centered like the rest of the header (business_name / address / tax_id);
    # the separator is full-width so centering only affects the venue lines.
    if receipt.establishment:
        printer.set(align="center")
        printer.text(_separator(width) + "\n")
        for line in receipt.establishment.splitlines():
            line = line.strip()
            if line:
                printer.text(line + "\n")

    # ---- Items ----
    printer.set(align="left", bold=False)
    printer.text(_separator(width) + "\n")
    for item in receipt.items:
        _render_item(printer, item, width)

    # ---- Payment ----
    printer.text(_separator(width) + "\n")
    printer.text(_two_col("Вид операції", receipt.operation, width) + "\n")
    printer.text(_two_col(receipt.payment_name, f"{_format_money(receipt.paid_sum)} грн", width) + "\n")
    if receipt.acquirer:
        a = receipt.acquirer
        if a.cardmask:
            printer.text(_two_col("Картка", a.cardmask, width) + "\n")
        if a.terminal_id:
            printer.text(_two_col("Термінал", a.terminal_id, width) + "\n")
        if a.rrn:
            printer.text(_two_col("RRN", a.rrn, width) + "\n")
        if a.auth_code:
            printer.text(_two_col("Код авторизації", a.auth_code, width) + "\n")
        if a.payment_date:
            printer.text(_two_col("Дата оплати", a.payment_date.strftime("%d.%m.%Y %H:%M:%S"), width) + "\n")
        if a.paysys:
            printer.text(_two_col("Платіжна система", a.paysys, width) + "\n")

    # ---- Total — emphasised ----
    printer.text(_separator(width) + "\n")
    printer.set(bold=True, double_height=True, double_width=False)
    printer.text(_two_col("СУМА", _format_money(receipt.total_sum), width) + "\n")
    printer.set(bold=False, double_height=False, double_width=False)

    # ---- Tax breakdown ----
    for tax in receipt.taxes:
        rate_part = f" {tax.rate:g}%" if tax.rate else ""
        symbol_part = f" {tax.symbol}" if tax.symbol else ""
        label = f"{tax.name}{rate_part}{symbol_part}".strip()
        printer.text(_two_col(label, _format_money(tax.value), width) + "\n")
    printer.text(_two_col("До сплати", f"{_format_money(receipt.total_sum)} грн", width) + "\n")

    # ---- Comment / footer ----
    if receipt.comment:
        printer.text(_separator(width) + "\n")
        printer.text("Коментар:\n")
        printer.text(receipt.comment + "\n")

    if receipt.footer:
        printer.text(_separator(width) + "\n")
        printer.set(align="center")
        for line in _wrap_lines(receipt.footer, width):
            printer.text(line + "\n")
        printer.set(align="left")

    # ---- Fiscal block ----
    printer.text(_separator(width) + "\n")
    if receipt.fiscal_number:
        printer.text(f"ЧЕК № {receipt.fiscal_number}\n")
    printer.text(receipt.fiscal_date.strftime("%d.%m.%Y %H:%M:%S") + "\n")
    if receipt.cashier:
        printer.text(f"Касир: {receipt.cashier}\n")

    # ---- QR code ----
    if receipt.qr_url:
        printer.text("\n")
        # Render the QR through PIL + the bitmap pipeline so it lands on the
        # paper centred regardless of the current alignment command — the
        # native printer.qr() bypasses our bitmap patch and was always
        # left-justified on this hardware.
        paper_w = 576 if width >= 48 else 384
        # Size the QR to FILL the paper width with the LARGEST integer
        # dots-per-module that still fits. The old ~160-dot target left only
        # 2-3 dots per module once the fiscal URL got long — cheap 58 mm
        # thermal heads smear modules that small into an unscannable blob
        # (80 mm / better heads still just barely read it). Bigger modules
        # scan reliably. box_size is an integer and box_size * modules_across
        # <= paper_w by construction, so we never resize (resizing a 1-bit QR
        # misaligns modules and is exactly what breaks scanning). The QR's
        # `border` already carries the required quiet zone.
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
        qr.add_data(receipt.qr_url)
        qr.make(fit=True)
        modules_across = qr.modules_count + qr.border * 2
        qr.box_size = _qr_box_size(modules_across, paper_w)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("1")
        # Centre on the paper. A small bottom gap is baked into the canvas
        # instead of a trailing LF: GS v 0 already feeds its own height, and
        # an extra "\n" would add a firmware-dependent feed (the same issue
        # fixed for text lines in the bitmap pipeline).
        bottom_gap = 8
        canvas = Image.new("1", (paper_w, qr_img.height + bottom_gap), 1)
        canvas.paste(qr_img, ((paper_w - qr_img.width) // 2, 0))
        printer._raw(image_to_gs_v_0(canvas))

    # ---- Pos footer ----
    printer.text(_separator(width) + "\n")
    printer.text(f"Режим роботи: {'Онлайн' if receipt.online_mode else 'Офлайн'}\n")
    if receipt.pos_fiscal_number:
        printer.text(f"ФН ПРРО {receipt.pos_fiscal_number}\n")
    printer.set(align="center", bold=True)
    printer.text(receipt.receipt_type + "\n")
    printer.set(align="left", bold=False)
    if receipt.operator:
        printer.set(align="center")
        printer.text(receipt.operator.replace("_", ".").upper() + "\n")
        printer.set(align="left")

    printer.text("\n\n")
    printer.cut()
