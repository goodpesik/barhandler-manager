"""PET-237 — connect an RT fiscal printer (or the fiscal_epos emulator) to the
manager by IP:port and fiscalize through it end-to-end.

Discovery can't find an HTTP fpmate device, so it's registered manually via
POST /devices/register-manual (mirrors /terminal/register-manual). This test
proves the whole path: register-manual → /fiscal/it/document → the emulator.
"""

from __future__ import annotations

import socket

from emulator import fiscal_epos


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_register_manual_then_fiscalize_through_manager(client, auth_headers):
    state = fiscal_epos.FiscalState()
    port = _free_port()
    server = fiscal_epos.start_server(state, "127.0.0.1", port)
    try:
        # 1. Register the emulator as a fiscal_it printer by IP:port.
        r = client.post(
            "/devices/register-manual",
            headers=auth_headers,
            json={
                "host": "127.0.0.1",
                "port": port,
                "kind": "fiscal_it",
                "nickname": "RT emulator",
            },
        )
        assert r.status_code == 200, r.text
        printer = r.json()["printer"]
        pid = printer["descriptor"]["id"]
        assert printer["kind"] == "fiscal_it"
        assert printer["descriptor"]["network"]["port"] == port

        # 2. Fiscalize a sale through the manager → routed to the emulator.
        doc = {
            "items": [
                {
                    "name": "Caffè",
                    "quantity": 1,
                    "unit_price": 2.2,
                    "total_price": 2.2,
                    "iva_rate": 22,
                    "department": 1,
                }
            ],
            "payment": {"type": "cash", "amount": 2.2},
        }
        r2 = client.post(
            f"/fiscal/it/document?printer_id={pid}",
            headers=auth_headers,
            json=doc,
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["receiptNumber"] == "1"
        assert state.snapshot()[0]["kind"] == "DOCUMENTO COMMERCIALE"
    finally:
        server.shutdown()


def test_fiscal_it_document_without_registered_printer_is_503(client, auth_headers):
    doc = {
        "items": [
            {
                "name": "X",
                "quantity": 1,
                "unit_price": 1,
                "total_price": 1,
                "iva_rate": 22,
            }
        ],
        "payment": {"type": "cash", "amount": 1},
    }
    r = client.post("/fiscal/it/document", headers=auth_headers, json=doc)
    assert r.status_code == 503


def test_reprint_prints_a_duplicate(client, auth_headers):
    from emulator import fiscal_epos

    state = fiscal_epos.FiscalState()
    port = _free_port()
    server = fiscal_epos.start_server(state, "127.0.0.1", port)
    try:
        client.post(
            "/devices/register-manual",
            headers=auth_headers,
            json={"host": "127.0.0.1", "port": port, "kind": "fiscal_it"},
        )
        doc = {
            "items": [{"name": "Caffè", "quantity": 1, "unit_price": 2.2,
                       "total_price": 2.2, "iva_rate": 22, "department": 1}],
            "payment": {"type": "cash", "amount": 2.2},
        }
        r = client.post("/fiscal/it/document", headers=auth_headers, json=doc)
        assert r.status_code == 200, r.text
        num = r.json()["receiptNumber"]
        # reprint the same receipt → the emulator records a COPIA, no new sale
        rr = client.post(
            "/fiscal/it/reprint", headers=auth_headers,
            json={"receipt_number": num},
        )
        assert rr.status_code == 200, rr.text
        assert state.snapshot()[0]["kind"] == "COPIA"
        assert state.receipt_number == 1  # reprint did NOT issue a new receipt
    finally:
        server.shutdown()


def test_status_after_register(client, auth_headers):
    from emulator import fiscal_epos
    state = fiscal_epos.FiscalState()
    port = _free_port()
    server = fiscal_epos.start_server(state, "127.0.0.1", port)
    try:
        reg = client.post("/devices/register-manual", headers=auth_headers,
                          json={"host": "127.0.0.1", "port": port, "kind": "fiscal_it"})
        pid = reg.json()["printer"]["descriptor"]["id"]
        rs = client.get(f"/fiscal/it/status?printer_id={pid}", headers=auth_headers)
        assert rs.status_code == 200
    finally:
        server.shutdown()


def test_endpoint_url_normalizes_scheme_host():
    """A printer registered with the URL as host (http://127.0.0.1) must not
    double-prefix to http://http://... — that produced 503 printer_unreachable."""
    from src.services import fiscal_it
    assert fiscal_it._endpoint_url("http://127.0.0.1", 8095).startswith(
        "http://127.0.0.1:8095/"
    )
    assert fiscal_it._endpoint_url("127.0.0.1", 8095).startswith(
        "http://127.0.0.1:8095/"
    )
    # embedded port in host is dropped (port passed separately)
    assert fiscal_it._endpoint_url("http://127.0.0.1:8095", 8095).startswith(
        "http://127.0.0.1:8095/"
    )
    assert "http://http" not in fiscal_it._endpoint_url("http://127.0.0.1", 8095)


def test_split_payment_reaches_the_printer_and_the_z_shows_both(
    client, auth_headers
):
    """BH-171 — перевірка рівно з тікета, повним шляхом через менеджер.

    Чек 100 = 30 карткою + 70 готівкою мусить закритись ДВОМА рядками оплати, а
    денний Z — показати обидва способи. RT рахує Z саме з цих рядків, тож поки
    весь чек ішов одним способом, готівка в касі не сходилась зі звіркою. Це
    фіскальний документ, тобто там була неправда.

    Це не греп по XML: документ справді летить у фіскальний емулятор, і
    перевіряється те, що емулятор із нього вичитав.
    """
    state = fiscal_epos.FiscalState()
    port = _free_port()
    server = fiscal_epos.start_server(state, "127.0.0.1", port)
    try:
        r = client.post(
            "/devices/register-manual",
            headers=auth_headers,
            json={
                "host": "127.0.0.1",
                "port": port,
                "kind": "fiscal_it",
                "nickname": "RT emulator",
            },
        )
        assert r.status_code == 200, r.text
        pid = r.json()["printer"]["descriptor"]["id"]

        doc = {
            "items": [
                {
                    "name": "Menu",
                    "quantity": 1,
                    "unit_price": 100.0,
                    "total_price": 100.0,
                    "iva_rate": 22,
                    "department": 1,
                }
            ],
            # Старе поле лишається — його читає менеджер старішої версії.
            "payment": {"type": "card", "amount": 100.0},
            "payments": [
                {"type": "card", "amount": 30.0},
                {"type": "cash", "amount": 70.0},
            ],
            "payment_type_map": {"card": 2, "cash": 0},
        }
        r2 = client.post(
            f"/fiscal/it/document?printer_id={pid}",
            headers=auth_headers,
            json=doc,
        )
        assert r2.status_code == 200, r2.text

        sale = state.snapshot()[0]
        assert sale["kind"] == "DOCUMENTO COMMERCIALE"
        # Сума чека — 100, а не 70: доти емулятор брав лише останній рядок.
        assert sale["total"] == "100.00"
        assert [(p["payment_type"], p["amount"]) for p in sale["payments"]] == [
            ("2", "30.00"),
            ("0", "70.00"),
        ]

        # І денний Z показує обидва способи, а не один на всю суму.
        rz = client.post("/fiscal/it/z", headers=auth_headers, json={})
        assert rz.status_code == 200, rz.text
        z_doc = next(d for d in state.snapshot() if d["kind"] == "CHIUSURA (Z)")
        assert z_doc["totals_by_payment_type"] == {"2": 30.0, "0": 70.0}
    finally:
        server.shutdown()


def test_a_receipt_without_a_split_still_prints_one_tender(client, auth_headers):
    """Старіший сервер переліку не надсилає — поводимось як раніше."""
    state = fiscal_epos.FiscalState()
    port = _free_port()
    server = fiscal_epos.start_server(state, "127.0.0.1", port)
    try:
        r = client.post(
            "/devices/register-manual",
            headers=auth_headers,
            json={
                "host": "127.0.0.1",
                "port": port,
                "kind": "fiscal_it",
                "nickname": "RT emulator",
            },
        )
        pid = r.json()["printer"]["descriptor"]["id"]

        r2 = client.post(
            f"/fiscal/it/document?printer_id={pid}",
            headers=auth_headers,
            json={
                "items": [
                    {
                        "name": "Caffè",
                        "quantity": 1,
                        "unit_price": 2.2,
                        "total_price": 2.2,
                        "iva_rate": 22,
                        "department": 1,
                    }
                ],
                "payment": {"type": "cash", "amount": 2.2},
            },
        )
        assert r2.status_code == 200, r2.text

        sale = state.snapshot()[0]
        assert len(sale["payments"]) == 1
        assert sale["total"] == "2.20"
    finally:
        server.shutdown()


def test_a_broken_split_is_refused_with_400_not_a_printer_error(
    client, auth_headers
):
    """Знайдено ревʼю BH-171: крива розбивка — це вада ЗАПИТУ, не принтера.

    Без цієї перевірки RT або дорахував би решту, або відмовився б закривати
    документ, і каса отримала б незрозумілий код принтера замість «сума оплат не
    дорівнює сумі чека». На фіскальному документі обидва варіанти неприйнятні.
    """
    state = fiscal_epos.FiscalState()
    port = _free_port()
    server = fiscal_epos.start_server(state, "127.0.0.1", port)
    try:
        r = client.post(
            "/devices/register-manual",
            headers=auth_headers,
            json={
                "host": "127.0.0.1",
                "port": port,
                "kind": "fiscal_it",
                "nickname": "RT emulator",
            },
        )
        pid = r.json()["printer"]["descriptor"]["id"]

        r2 = client.post(
            f"/fiscal/it/document?printer_id={pid}",
            headers=auth_headers,
            json={
                "items": [
                    {
                        "name": "Menu",
                        "quantity": 1,
                        "unit_price": 100.0,
                        "total_price": 100.0,
                        "iva_rate": 22,
                        "department": 1,
                    }
                ],
                "payment": {"type": "card", "amount": 100.0},
                # 30 + 40 = 70, а чек на 100.
                "payments": [
                    {"type": "card", "amount": 30.0},
                    {"type": "cash", "amount": 40.0},
                ],
                "payment_type_map": {"card": 2, "cash": 0},
            },
        )

        assert r2.status_code == 400, r2.text
        assert r2.json()["detail"]["code"] == "payments_sum_mismatch"
        # І на принтер нічого не поїхало — документа немає.
        assert state.snapshot() == []
    finally:
        server.shutdown()
