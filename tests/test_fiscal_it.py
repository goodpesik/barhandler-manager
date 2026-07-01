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
