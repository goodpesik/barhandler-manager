"""Italian RT fiscal builder + response parsing (PET-237 Phase C).

Pure-unit tests — no sockets. The XML builder is golden-tested (structure +
the load-bearing attributes: department mapping, cash rounding, refund vs sale,
payment-type mapping); the response parser is tested for success extraction and
error mapping, especially PRINTER ERROR 17 (first Z not run).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from src.services import fiscal_it


def _doc_xml(**kwargs) -> ET.Element:
    items = kwargs.pop(
        "items",
        [fiscal_it.ItItem(name="Espresso", quantity=2, unit_price=1.10, total_price=2.20, iva_rate=10.0, department=2)],
    )
    payment = kwargs.pop("payment", fiscal_it.ItPayment(type="cash", amount=2.20))
    return ET.fromstring(fiscal_it.build_commercial_document_xml(items, payment, **kwargs))


# ---------------------------------------------------------------------------
# XML builder — golden structure
# ---------------------------------------------------------------------------


def test_document_xml_golden_sale() -> None:
    root = _doc_xml()
    assert root.tag == "printerFiscalReceipt"
    tags = [c.tag for c in root]
    assert tags == ["beginFiscalReceipt", "printRecItem", "printRecTotal", "endFiscalReceipt"]

    item = root.find("printRecItem")
    assert item.get("description") == "Espresso"
    assert item.get("quantity") == "2.000"
    assert item.get("unitPrice") == "1.10"
    assert item.get("department") == "2"

    total = root.find("printRecTotal")
    assert total.get("payment") == "2.20"
    assert total.get("paymentType") == "0"  # cash


def test_department_falls_back_to_iva_map() -> None:
    # No explicit department → derived from the 22% IVA rate (reparto 1).
    item = fiscal_it.ItItem(name="Beer", quantity=1, unit_price=5.0, total_price=5.0, iva_rate=22.0, department=None)
    root = ET.fromstring(
        fiscal_it.build_commercial_document_xml([item], fiscal_it.ItPayment(type="cash", amount=5.0))
    )
    assert root.find("printRecItem").get("department") == "1"


def test_cash_total_is_rounded_to_5_cents() -> None:
    # 2.23 cash → rounds to 2.25.
    root = _doc_xml(payment=fiscal_it.ItPayment(type="cash", amount=2.23))
    assert root.find("printRecTotal").get("payment") == "2.25"


def test_card_total_is_not_rounded_and_uses_mapped_type() -> None:
    root = _doc_xml(
        payment=fiscal_it.ItPayment(type="card", amount=2.23),
        payment_type_map={"card": 2},
    )
    total = root.find("printRecTotal")
    assert total.get("payment") == "2.23"      # non-cash: exact, no rounding
    assert total.get("paymentType") == "2"     # mapped


def test_refund_uses_printRecRefund() -> None:
    root = _doc_xml(is_refund=True)
    assert root.find("printRecRefund") is not None
    assert root.find("printRecItem") is None
    # A reso is prefixed with a messageType="4" "RESO MERCE" header.
    msg = root.find("printRecMessage")
    assert msg is not None and msg.get("messageType") == "4"


def test_refund_reference_is_printed() -> None:
    item = fiscal_it.ItItem(name="Beer", quantity=1, unit_price=5.0, total_price=5.0, iva_rate=22.0)
    root = ET.fromstring(
        fiscal_it.build_commercial_document_xml(
            [item], fiscal_it.ItPayment(type="cash", amount=5.0),
            is_refund=True, refund_reference="RESO MERCE N.0007-0042 del 01/07/2026",
        )
    )
    assert root.find("printRecMessage").get("message") == "RESO MERCE N.0007-0042 del 01/07/2026"


def test_round_to_5_cents_edges() -> None:
    assert fiscal_it.round_to_5_cents(2.22) == 2.20
    assert fiscal_it.round_to_5_cents(2.23) == 2.25
    assert fiscal_it.round_to_5_cents(2.25) == 2.25
    assert fiscal_it.round_to_5_cents(0.0) == 0.0


def test_z_and_x_report_xml() -> None:
    z = ET.fromstring(fiscal_it.build_z_report_xml())
    assert z.tag == "printerFiscalReport"
    assert z.find("printZReport") is not None
    x = ET.fromstring(fiscal_it.build_x_report_xml())
    assert x.find("printXReport") is not None


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_parse_success_extracts_fields() -> None:
    xml = (
        '<response success="true" code="" status="0">'
        "<addInfo>"
        "<fiscalReceiptNumber>0042</fiscalReceiptNumber>"
        "<zRepNumber>0007</zRepNumber>"
        "</addInfo>"
        "</response>"
    )
    parsed = fiscal_it.parse_response(xml)
    assert parsed["success"] is True
    assert parsed["fields"]["fiscalReceiptNumber"] == "0042"

    # receiptId combines zRep + receipt number.
    rid, rnum = fiscal_it._extract_receipt_ids(parsed["fields"])
    assert rnum == "0042"
    assert rid == "0007-0042"


def test_parse_error_code_17_is_mapped() -> None:
    xml = '<response success="false" code="17" status="RT locked"></response>'
    with pytest.raises(fiscal_it.FiscalItError) as ei:
        fiscal_it.parse_response(xml)
    assert ei.value.code == "rt_17"
    # Error 17 = "IMPOSSIBILE ORA" (generic state error); the message names the
    # common first-Z cause without hard-asserting it.
    assert "IMPOSSIBILE ORA" in str(ei.value)
    assert "first daily closure" in str(ei.value).lower()


def test_parse_error_17_in_status_text() -> None:
    xml = '<response success="false" code="" status="PRINTER ERROR 17"></response>'
    with pytest.raises(fiscal_it.FiscalItError) as ei:
        fiscal_it.parse_response(xml)
    assert "PRINTER ERROR 17" in str(ei.value)


def test_parse_generic_error() -> None:
    xml = '<response success="false" code="42" status="paper end"></response>'
    with pytest.raises(fiscal_it.FiscalItError) as ei:
        fiscal_it.parse_response(xml)
    assert ei.value.code == "rt_42"
    assert "42" in str(ei.value)


def test_parse_soap_wrapped_response() -> None:
    xml = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        '<response success="true" code="" status="0"><addInfo>'
        "<fiscalReceiptNumber>0100</fiscalReceiptNumber></addInfo></response>"
        "</s:Body></s:Envelope>"
    )
    parsed = fiscal_it.parse_response(xml)
    assert parsed["fields"]["fiscalReceiptNumber"] == "0100"


def test_parse_bad_xml_raises() -> None:
    with pytest.raises(fiscal_it.FiscalItError) as ei:
        fiscal_it.parse_response("not xml <<<")
    assert ei.value.code == "bad_response"


# ---------------------------------------------------------------------------
# BH-171 — перелік оплат
# ---------------------------------------------------------------------------
#
# Чек можна оплатити кількома способами одразу: частина карткою, решта
# готівкою. RT рахує денний Z саме з рядків `printRecTotal`, тож коли весь чек
# іде одним способом, готівка в касі не сходиться зі звіркою. Це фіскальний
# документ, тобто там неправда, а не лише незручність.


def _totals(root: ET.Element) -> list[tuple[str, str, str]]:
    """(тип оплати, сума, індекс) кожного рядка оплати — по порядку."""
    return [
        (t.get("paymentType"), t.get("payment"), t.get("index"))
        for t in root.findall("printRecTotal")
    ]


def test_payments_list_closes_the_document_with_a_line_per_tender() -> None:
    """Перевірка з тікета: 100 = 30 карткою + 70 готівкою → ДВА рядки оплати."""
    root = _doc_xml(
        items=[
            fiscal_it.ItItem(
                name="Menu", quantity=1, unit_price=100.0,
                total_price=100.0, iva_rate=10.0, department=2,
            )
        ],
        payment=fiscal_it.ItPayment(type="card", amount=100.0),
        payments=[
            fiscal_it.ItPayment(type="card", amount=30.0),
            fiscal_it.ItPayment(type="cash", amount=70.0),
        ],
        payment_type_map={"card": 2, "cash": 0},
    )

    assert _totals(root) == [("2", "30.00", "1"), ("0", "70.00", "2")]


def test_the_split_sums_to_the_document_total() -> None:
    """Найдорожче: якщо сума рядків не дорівнює сумі чека, RT або дорахує
    решту, або відмовиться закрити документ."""
    root = _doc_xml(
        items=[
            fiscal_it.ItItem(
                name="Menu", quantity=1, unit_price=100.0,
                total_price=100.0, iva_rate=10.0, department=2,
            )
        ],
        payment=fiscal_it.ItPayment(type="cash", amount=100.0),
        payments=[
            fiscal_it.ItPayment(type="card", amount=30.0),
            fiscal_it.ItPayment(type="cash", amount=70.0),
        ],
        payment_type_map={"card": 2, "cash": 0},
    )

    assert sum(float(amount) for _t, amount, _i in _totals(root)) == 100.0


def test_a_split_that_does_not_add_up_is_refused() -> None:
    """Знайдено ревʼю: доти сума рядків ніде не звірялась із сумою чека, і
    документ спокійно збирався. Відмовити тут краще, ніж дати RT дорахувати
    решту або відмовитись закривати — каса побачила б незрозумілий код."""
    with pytest.raises(fiscal_it.FiscalItPayloadError) as exc:
        _doc_xml(
            items=[
                fiscal_it.ItItem(
                    name="Menu", quantity=1, unit_price=100.0,
                    total_price=100.0, iva_rate=10.0, department=2,
                )
            ],
            payments=[
                fiscal_it.ItPayment(type="card", amount=30.0),
                fiscal_it.ItPayment(type="cash", amount=40.0),
            ],
        )

    assert exc.value.code == "payments_sum_mismatch"


def test_a_split_with_an_unrounded_cash_line_is_refused() -> None:
    """Округлювати готівку в розбивці не можна — це зсунуло б суму від суми
    чека. Але й пропускати некратну готівку не можна: касир фізично віддасть
    суму, кратну 5 копійкам, а в документі стоятиме інша, і звірка готівки
    знову не зійдеться. Тобто вада, яку BH-171 лікує, лишилась би — просто
    меншого розміру. Кратність забезпечує той, хто розбивку склав."""
    with pytest.raises(fiscal_it.FiscalItPayloadError) as exc:
        _doc_xml(
            items=[
                fiscal_it.ItItem(
                    name="Menu", quantity=1, unit_price=100.0,
                    total_price=100.0, iva_rate=10.0, department=2,
                )
            ],
            payments=[
                fiscal_it.ItPayment(type="card", amount=29.97),
                fiscal_it.ItPayment(type="cash", amount=70.03),
            ],
        )

    assert exc.value.code == "cash_not_rounded"


def test_a_split_whose_cash_part_is_rounded_passes() -> None:
    root = _doc_xml(
        items=[
            fiscal_it.ItItem(
                name="Menu", quantity=1, unit_price=100.0,
                total_price=100.0, iva_rate=10.0, department=2,
            )
        ],
        payments=[
            fiscal_it.ItPayment(type="card", amount=29.95),
            fiscal_it.ItPayment(type="cash", amount=70.05),
        ],
        payment_type_map={"card": 2, "cash": 0},
    )

    assert _totals(root) == [("2", "29.95", "1"), ("0", "70.05", "2")]


def test_single_cash_tender_in_a_list_still_rounds() -> None:
    """Один спосіб у переліку — це той самий випадок, що й без переліку, і
    округлення там законне."""
    root = _doc_xml(
        payment=fiscal_it.ItPayment(type="cash", amount=2.23),
        payments=[fiscal_it.ItPayment(type="cash", amount=2.23)],
    )

    assert _totals(root) == [("0", "2.25", "1")]


def test_no_payments_list_behaves_exactly_as_before() -> None:
    """Старіший сервер переліку не надсилає — там єдиний `payment`."""
    root = _doc_xml(payment=fiscal_it.ItPayment(type="cash", amount=2.23))

    assert _totals(root) == [("0", "2.25", "1")]


def test_an_empty_payments_list_falls_back_to_the_single_payment() -> None:
    """`payments: []` це «розбивки немає», а не «оплат немає»: закрити документ
    без жодного рядка оплати неможливо."""
    root = _doc_xml(
        payment=fiscal_it.ItPayment(type="cash", amount=2.20), payments=[]
    )

    assert _totals(root) == [("0", "2.20", "1")]


def test_each_tender_is_named_in_italian_on_the_receipt() -> None:
    """Знайдено ревʼю: у рядку оплати стояв СИРИЙ ключ від каси («card»,
    «cash»), тобто на італійському чеку, який читає клієнт і податкова, було
    англійське слово. Поки спосіб був один — одна дивна назва; з розбивкою вона
    множиться на кожен рядок.

    І назви мусять РОЗРІЗНЯТИСЬ, інакше людина не прочитає, що чим оплачено.
    """
    root = _doc_xml(
        payments=[
            fiscal_it.ItPayment(type="card", amount=1.00),
            fiscal_it.ItPayment(type="cash", amount=1.20),
        ],
        payment_type_map={"card": 2, "cash": 0},
    )
    descriptions = [t.get("description") for t in root.findall("printRecTotal")]

    assert descriptions == ["Carta", "Contanti"]


def test_an_unknown_tender_is_named_by_what_it_is_recorded_as() -> None:
    """Невідомий ключ резолвиться в готівку, тож і називатись мусить готівкою:
    саме так він і піде в денний Z. Друкувати «незнайомий» у фіскальному
    документі було б неправдою про те, як його врахували."""
    root = _doc_xml(payment=fiscal_it.ItPayment(type="незнайомий", amount=2.20))

    assert [
        t.get("description") for t in root.findall("printRecTotal")
    ] == ["Contanti"]


def test_a_refund_with_a_split_marks_every_line_as_refund() -> None:
    root = _doc_xml(
        is_refund=True,
        payments=[
            fiscal_it.ItPayment(type="card", amount=1.00),
            fiscal_it.ItPayment(type="cash", amount=1.20),
        ],
    )
    descriptions = [t.get("description") for t in root.findall("printRecTotal")]

    assert descriptions == ["RIMBORSO", "RIMBORSO"]


def _split_error(payments, payment_type_map=None) -> str:
    """Код відмови на розбивку, яку не можна віддати принтеру."""
    with pytest.raises(fiscal_it.FiscalItPayloadError) as err:
        _doc_xml(payments=payments, payment_type_map=payment_type_map)
    return err.value.code


def test_an_unknown_tender_in_a_split_is_rejected_not_printed_as_cash() -> None:
    """Третім колом ревʼю: невідомий ключ у розбивці мовчки ставав готівкою.

    Ваучер друкувався як «Contanti» на фіскальному документі й рахувався в
    готівку при звірці — рівно та вада, яку лікує BH-171. А з некратною сумою
    він ще й відхиляв ЗАКОННИЙ чек як «некратну готівку»."""
    assert _split_error(
        [
            fiscal_it.ItPayment(type="buoni_pasto", amount=0.97),
            fiscal_it.ItPayment(type="cash", amount=1.23),
        ],
        payment_type_map={"cash": 0, "card": 2},
    ) == "unknown_payment_type"


def test_a_single_unknown_tender_still_degrades_to_cash() -> None:
    """Сумісність: на ОДНОМУ способі чек закривається однією сумою, і RT її
    приймає — тут запасна готівка лишається, як була."""
    root = _doc_xml(
        payments=[fiscal_it.ItPayment(type="незнайомий", amount=2.20)],
        payment_type_map={"cash": 0},
    )

    assert [t for t, _a, _i in _totals(root)] == ["0"]


@pytest.mark.parametrize("bad", [0.0, -50.0])
def test_a_zero_or_negative_part_is_rejected_even_when_the_sum_matches(bad) -> None:
    """150 готівкою плюс -50 карткою дають ту саму суму чека. Сума сходиться,
    а рядок оплати неможливий — RT на ньому поведеться непередбачувано."""
    total = 2.20
    assert _split_error(
        [
            fiscal_it.ItPayment(type="card", amount=bad),
            fiscal_it.ItPayment(type="cash", amount=round(total - bad, 2)),
        ],
    ) == "invalid_tender_amount"


def test_a_custom_cash_key_is_held_to_the_5_cent_rule() -> None:
    """Регресія на «спільну мапу» (друге коло): кастомний ключ каси, замапений
    у готівку, мусить підпадати під кратність 5 копійкам. Повна відмова від
    мапи в перевірці проходила всі тести — цей її ловить."""
    assert _split_error(
        [
            fiscal_it.ItPayment(type="efectivo", amount=1.17),
            fiscal_it.ItPayment(type="tarjeta", amount=1.03),
        ],
        payment_type_map={"efectivo": 0, "tarjeta": 2},
    ) == "cash_not_rounded"


def test_a_literal_cash_key_remapped_to_card_is_not_treated_as_cash() -> None:
    """Дзеркало: ключ «cash», який каса замапила в картку, — не готівка, і
    некратна сума тут законна."""
    root = _doc_xml(
        payments=[
            fiscal_it.ItPayment(type="cash", amount=1.17),
            fiscal_it.ItPayment(type="card", amount=1.03),
        ],
        payment_type_map={"cash": 2, "card": 2},
    )

    assert [t for t, _a, _i in _totals(root)] == ["2", "2"]


def test_indexes_are_sequential_from_one() -> None:
    """RT звіряє рядки оплати за індексом; повтор або нуль ламає закриття."""
    root = _doc_xml(
        payments=[
            fiscal_it.ItPayment(type="cash", amount=amount)
            for amount in (1.00, 0.70, 0.50)
        ],
    )

    assert [i for _t, _a, i in _totals(root)] == ["1", "2", "3"]
