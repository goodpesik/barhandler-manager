"""PET-882 — choosing between Void and Refund on the terminal.

Обидві операції повертають гроші, але Void живе лише до звірки підсумків
і лише на тому самому терміналі того самого дня. Помилитись тут означає,
що касир дізнається про відмову вже перед людиною.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from src.services.terminals.refund_choice import (
    RefundContext,
    choose_refund_operation,
)

TODAY = date(2026, 9, 22)


def ctx(**over) -> RefundContext:
    base = dict(
        paid_at=datetime(2026, 9, 22, 11, 30),
        today=TODAY,
        invoice_num="000123",
        paid_terminal_id="T1",
        current_terminal_id="T1",
        full_amount=True,
        settled=False,
    )
    base.update(over)
    return RefundContext(**base)


class TestWhenVoidIsAllowed:
    def test_same_day_same_terminal_full_amount_is_a_void(self) -> None:
        assert choose_refund_operation(ctx()) == "Void"

    def test_part_of_the_same_day_sale_is_a_partial_void(self) -> None:
        assert choose_refund_operation(ctx(full_amount=False)) == "PartialVoid"


class TestWhenOnlyRefundWorks:
    def test_after_the_totals_were_settled(self) -> None:
        # Скасовувати вже нема що: операція пішла в банк підсумком.
        assert choose_refund_operation(ctx(settled=True)) == "Refund"

    def test_a_sale_from_another_day(self) -> None:
        assert (
            choose_refund_operation(ctx(paid_at=datetime(2026, 9, 21, 18, 0)))
            == "Refund"
        )

    def test_another_terminal(self) -> None:
        assert choose_refund_operation(ctx(current_terminal_id="T2")) == "Refund"

    def test_no_receipt_number_to_cancel_by(self) -> None:
        for empty in (None, ""):
            assert choose_refund_operation(ctx(invoice_num=empty)) == "Refund"

    def test_we_do_not_know_when_it_was_paid(self) -> None:
        assert choose_refund_operation(ctx(paid_at=None)) == "Refund"

    @pytest.mark.parametrize("field", ["paid_terminal_id", "current_terminal_id"])
    def test_we_do_not_know_which_terminal(self, field: str) -> None:
        assert choose_refund_operation(ctx(**{field: None})) == "Refund"


class TestUncertaintyFallsBackToRefund:
    """Кожна невизначеність веде до Refund: він працює завжди, коли є
    номери оплати, а Void у сумнівному випадку просто відмовить."""

    def test_settlement_unknown_is_enough_on_its_own(self) -> None:
        """Раніше тут стояло протилежне — тест фіксував ваду як правильну
        поведінку. Звірка могла вже пройти, а прапорець просто ніхто не
        встиг виставити: тоді Void відмовляє вже перед людиною."""
        assert choose_refund_operation(ctx(settled=None)) == "Refund"

    def test_void_needs_to_KNOW_there_was_no_settlement(self) -> None:
        assert choose_refund_operation(ctx(settled=False)) == "Void"

    def test_every_single_mismatch_is_enough_on_its_own(self) -> None:
        """Жодна з умов не «перекривається» іншою: приберемо по одній —
        і щоразу маємо Refund. Інакше зайва умова тихо нічого не робила б."""
        for over in (
            {"settled": True},
            {"settled": None},
            {"paid_at": datetime(2026, 9, 20, 9, 0)},
            {"invoice_num": None},
            {"paid_terminal_id": None},
            {"current_terminal_id": "T9"},
        ):
            assert choose_refund_operation(ctx(**over)) == "Refund", over


class TestEveryAdapterAnswersTheSameRefundContract:
    """PET-882 — спільний маршрут `/terminal/refund` кличе `adapter.refund`
    ОДНИМ позиційним аргументом. Адаптер із власним `refund` іншої форми
    тихо перекриває базовий, і виклик падає `TypeError` — тобто 500 замість
    зрозумілого «проведіть вручну». Саме так і сталося з PrivatBank, поки
    ревʼю це не знайшло; перевіряємо ВСІ адаптери, а не той один."""

    def test_signature_matches_the_base_everywhere(self) -> None:
        import inspect

        from src.services.terminals.base import TerminalAdapter
        from src.services.terminals.bpos import BposTerminalAdapter
        from src.services.terminals.oschad import OschadTerminalAdapter
        from src.services.terminals.posapi import PosApiTerminalAdapter
        from src.services.terminals.privatbank import PrivatBankTerminalAdapter
        from src.services.terminals.ssi import SSITerminalAdapter

        adapters = [
            BposTerminalAdapter,
            OschadTerminalAdapter,
            PosApiTerminalAdapter,
            PrivatBankTerminalAdapter,
            SSITerminalAdapter,
        ]
        # Перелік іменований, а не виведений з модуля: інакше тест погодився
        # б із тим, що якийсь адаптер просто зник із перевірки.
        assert len(adapters) == 5

        base = list(inspect.signature(TerminalAdapter.refund).parameters)
        for cls in adapters:
            got = list(inspect.signature(cls.refund).parameters)
            assert got == base, f"{cls.__name__}.refund{tuple(got)}"
