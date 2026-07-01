"""End-to-end + contract tests for the Fiscal ePOS-Print emulator (PET-237).

These drive the REAL manager client (`src.services.fiscal_it`) against the
emulated Epson RT printer over HTTP — so the fiscal ePOS-Print XML request AND
the `<response>` round-trip through the actual builder + parser. The schema is
exercised end-to-end, not merely golden-asserted.
"""

from __future__ import annotations

import socket

import pytest

from emulator import fiscal_epos
from src.services import fiscal_it


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sale_doc(amount: float = 2.20) -> fiscal_it.ItDocument:
    return fiscal_it.ItDocument(
        items=[
            fiscal_it.ItItem(
                name="Caffè", quantity=1, unit_price=amount,
                total_price=amount, iva_rate=22.0, department=1,
            )
        ],
        payment=fiscal_it.ItPayment(type="cash", amount=amount),
    )


@pytest.fixture
def emulator_server():
    state = fiscal_epos.FiscalState()
    port = _free_port()
    server = fiscal_epos.start_server(state, "127.0.0.1", port)
    try:
        yield state, port
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# End-to-end over HTTP
# ---------------------------------------------------------------------------


def test_e2e_sale_round_trips(emulator_server) -> None:
    state, port = emulator_server
    r1 = fiscal_it.print_commercial_document("127.0.0.1", _sale_doc(), port=port)
    assert r1["receiptNumber"] == "1"
    assert r1["raw"]["success"] is True

    r2 = fiscal_it.print_commercial_document("127.0.0.1", _sale_doc(), port=port)
    assert r2["receiptNumber"] == "2"

    docs = state.snapshot()
    assert docs[0]["kind"] == "DOCUMENTO COMMERCIALE"
    assert docs[0]["items"][0]["description"] == "Caffè"


def test_e2e_first_z_required_returns_error_17() -> None:
    state = fiscal_epos.FiscalState(require_first_z=True)
    port = _free_port()
    server = fiscal_epos.start_server(state, "127.0.0.1", port)
    try:
        # Sale before the day's first Z → error 17.
        with pytest.raises(fiscal_it.FiscalItError) as ei:
            fiscal_it.print_commercial_document("127.0.0.1", _sale_doc(), port=port)
        assert ei.value.code == "rt_17"

        # Run the first Z, then the same sale succeeds.
        z = fiscal_it.run_z_report("127.0.0.1", port=port)
        assert z["reportNumber"] == "0001"
        r = fiscal_it.print_commercial_document("127.0.0.1", _sale_doc(), port=port)
        assert r["receiptNumber"] == "1"
    finally:
        server.shutdown()


def test_e2e_refund_round_trips(emulator_server) -> None:
    state, port = emulator_server
    doc = _sale_doc()
    doc.is_refund = True
    r = fiscal_it.print_commercial_document("127.0.0.1", doc, port=port)
    assert r["receiptNumber"] == "1"
    assert state.snapshot()[0]["kind"] == "RESO"


def test_e2e_z_x_and_status(emulator_server) -> None:
    state, port = emulator_server
    assert fiscal_it.run_z_report("127.0.0.1", port=port)["reportNumber"] == "0001"
    assert fiscal_it.run_x_report("127.0.0.1", port=port)["reportNumber"] == "0001"
    st = fiscal_it.query_status("127.0.0.1", port=port)
    assert st["blocked"] is False
    assert st["lastZAt"] is not None  # a Z was just run


# ---------------------------------------------------------------------------
# Direct handler contract (no sockets)
# ---------------------------------------------------------------------------


def test_handler_sale_response_parses() -> None:
    state = fiscal_epos.FiscalState()
    body = fiscal_it._soap_wrap(
        fiscal_it.build_commercial_document_xml(
            [fiscal_it.ItItem(name="X", quantity=1, unit_price=1.0, total_price=1.0, iva_rate=22.0, department=1)],
            fiscal_it.ItPayment(type="cash", amount=1.0),
        )
    )
    parsed = fiscal_it.parse_response(fiscal_epos.handle_request(state, body))
    assert parsed["success"] is True
    assert parsed["fields"]["fiscalReceiptNumber"] == "1"


def test_handler_unknown_command_is_error() -> None:
    state = fiscal_epos.FiscalState()
    resp = fiscal_epos.handle_request(state, "<foo/>")
    with pytest.raises(fiscal_it.FiscalItError):
        fiscal_it.parse_response(resp)
