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
