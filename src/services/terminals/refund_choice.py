"""PET-882 — ЯКОЮ ОПЕРАЦІЄЮ ПОВЕРТАТИ ГРОШІ НА ТЕРМІНАЛІ.

Протокол дає дві різні операції, і вибір між ними не косметичний:

* ``Void`` / ``PartialVoid`` скасовує операцію ДНЯ за номером чека на
  терміналі. Дешевша для закладу (банк не бере комісію за скасовану
  операцію) і швидша, але живе лише до звірки підсумків.
* ``Refund`` — окрема операція повернення за rrn/authCode. Днем не
  обмежена, тож працює завжди, коли є ці номери.

Правило тримається тут, окремо від адаптера: помилка в ньому коштує
грошей, а перевірити чисту функцію можна на кожній гілці.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Optional

RefundOperation = Literal["Void", "PartialVoid", "Refund"]


@dataclass(frozen=True)
class RefundContext:
    """Усе, що впливає на вибір. Нічого зайвого."""

    #: Коли пройшла початкова оплата.
    paid_at: Optional[datetime]
    #: «Сьогодні» з точки зору касового дня.
    today: date
    #: Номер чека на терміналі; без нього скасувати нема що.
    invoice_num: Optional[str]
    #: Термінал, на якому платили, і термінал, що перед нами зараз.
    paid_terminal_id: Optional[str]
    current_terminal_id: Optional[str]
    #: Чи повертаємо всю суму оплати.
    full_amount: bool
    #: Чи вже робили звірку підсумків після тієї оплати. ``None`` — не знаємо,
    #: і це веде до ``Refund``: невідоме тут коштує відмови перед людиною.
    settled: Optional[bool] = None


def choose_refund_operation(ctx: RefundContext) -> RefundOperation:
    """Операція, якою варто повертати ці гроші.

    ``Refund`` — БЕЗПЕЧНЕ ЗАМОВЧУВАННЯ, і кожна невизначеність веде саме
    до нього: воно працює завжди, коли є номери оплати, тоді як
    ``Void`` після звірки просто відмовить, і касир дізнається про це
    вже перед людиною.

    Умови записані ДОЗВІЛЬНО: ``Void`` дозволений лише тоді, коли ВСЕ
    збіглось — той самий день, той самий термінал, є номер чека і звірки
    ще не було. Заборонна форма («усе, крім…») пропускала б кожен новий
    випадок, який колись зʼявиться.
    """
    # НЕ `is True`: «не знаємо» — теж не привід скасовувати. Void після
    # звірки просто відмовить, і касир дізнається про це вже перед людиною,
    # тож дозволяємо його ЛИШЕ коли достеменно відомо, що звірки не було
    # (знайшло ревʼю: докстрінг обіцяв саме це, а код пускав `None`).
    if ctx.settled is not False:
        return "Refund"
    if not ctx.invoice_num:
        return "Refund"
    if ctx.paid_at is None:
        return "Refund"
    if ctx.paid_at.date() != ctx.today:
        return "Refund"
    if not ctx.paid_terminal_id or not ctx.current_terminal_id:
        return "Refund"
    if ctx.paid_terminal_id != ctx.current_terminal_id:
        return "Refund"
    return "Void" if ctx.full_amount else "PartialVoid"
