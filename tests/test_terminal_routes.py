"""HTTP surface for /terminal/* — discover, register, charge, ping, etc.

These tests run the real FastAPI app in-process via TestClient. We
monkey-patch the LAN-scan hook and the SSI adapter so no real socket
is opened — the goal is to verify the route shapes, error mapping
(503 detail.code envelope), and registry wiring, not the protocol
itself (that's `test_ssi_adapter.py`).
"""

from __future__ import annotations

from typing import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.models.terminal import (
    AcquirerResult,
    TerminalDescriptor,
    TerminalKind,
    TerminalNetworkAddress,
    TerminalTransport,
)
from src.server import create_app
from src.services.terminals.base import MerchantInfo, TerminalUnavailable


@pytest.fixture
def fake_terminal() -> TerminalDescriptor:
    return TerminalDescriptor(
        id="abc123def456",
        transport=TerminalTransport.network,
        label="Mono POS @ 10.0.0.42",
        kind=TerminalKind.mono_pos,
        model="Verifone X990",
        serial="V1E0207420",
        network=TerminalNetworkAddress(host="10.0.0.42", port=3000),
    )


@pytest.fixture
def client_with_terminal(
    config: dict, fake_terminal: TerminalDescriptor,
) -> Iterator[TestClient]:
    """Stub LAN scan to return one terminal so /discover + /register work."""
    with patch("src.devices.scan.discover_usb", return_value=[]), \
         patch("src.devices.scan.discover_network", return_value=[]), \
         patch("src.devices.scan.discover_bluetooth", return_value=[]), \
         patch(
             "src.devices.scan.discover_network_terminals",
             return_value=[fake_terminal],
         ):
        app = create_app(config)
        with TestClient(app) as c:
            yield c


def test_discover_returns_fake_terminal(
    client_with_terminal: TestClient, auth_headers: dict, fake_terminal,
) -> None:
    response = client_with_terminal.post("/terminal/discover", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["terminals"]) == 1
    assert body["terminals"][0]["id"] == fake_terminal.id
    assert body["terminals"][0]["kind"] == "mono_pos"


def test_register_then_list_then_unregister(
    client_with_terminal: TestClient, auth_headers: dict, fake_terminal,
) -> None:
    client_with_terminal.post("/terminal/discover", headers=auth_headers)
    reg = client_with_terminal.post(
        "/terminal/register",
        headers=auth_headers,
        json={
            "id": fake_terminal.id,
            "kind": "mono_pos",
            "nickname": "Бар",
            "default_merchant_id": "000000060007176",
        },
    )
    assert reg.status_code == 200
    assert reg.json()["terminal"]["nickname"] == "Бар"

    listed = client_with_terminal.get("/terminal", headers=auth_headers).json()
    assert len(listed["terminals"]) == 1

    deleted = client_with_terminal.delete(
        f"/terminal/{fake_terminal.id}", headers=auth_headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "unregistered"


def test_register_unknown_id_returns_404(
    client_with_terminal: TestClient, auth_headers: dict,
) -> None:
    response = client_with_terminal.post(
        "/terminal/register",
        headers=auth_headers,
        json={"id": "never-discovered", "kind": "mono_pos"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "unknown_terminal"


def test_charge_without_registered_terminal_returns_no_terminal(
    client_with_terminal: TestClient, auth_headers: dict,
) -> None:
    """Frontend hitting /terminal/charge before the operator has
    registered anything must get the structured `no_terminal` 503 so
    the UI can show 'no POS terminal configured'."""
    response = client_with_terminal.post(
        "/terminal/charge",
        headers=auth_headers,
        json={"amount_kopecks": 100},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "no_terminal"


def _register_default(client: TestClient, headers: dict, terminal_id: str) -> None:
    client.post("/terminal/discover", headers=headers)
    client.post(
        "/terminal/register",
        headers=headers,
        json={
            "id": terminal_id,
            "kind": "mono_pos",
            "default_merchant_id": "000000060007176",
        },
    )


def test_ping_endpoint_calls_adapter_ping(
    client_with_terminal: TestClient, auth_headers: dict, fake_terminal,
) -> None:
    _register_default(client_with_terminal, auth_headers, fake_terminal.id)
    with patch(
        "src.services.terminals.ssi.SSITerminalAdapter.ping",
        new=AsyncMock(return_value=True),
    ):
        response = client_with_terminal.post(
            f"/terminal/{fake_terminal.id}/ping", headers=auth_headers,
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "terminal_id": fake_terminal.id}


def test_merchants_endpoint_returns_list(
    client_with_terminal: TestClient, auth_headers: dict, fake_terminal,
) -> None:
    _register_default(client_with_terminal, auth_headers, fake_terminal.id)
    fake_merchants = [
        MerchantInfo("M1", "T1", "ФОП Левинець"),
        MerchantInfo("M2", "T1", "ТОВ Smile Bar"),
    ]
    with patch(
        "src.services.terminals.ssi.SSITerminalAdapter.list_merchants",
        new=AsyncMock(return_value=fake_merchants),
    ):
        response = client_with_terminal.get(
            f"/terminal/{fake_terminal.id}/merchants", headers=auth_headers,
        )
    assert response.status_code == 200
    body = response.json()
    assert [m["merchant_id"] for m in body["merchants"]] == ["M1", "M2"]
    # Bank-side merchantName carries through; nickname starts blank.
    assert body["merchants"][0]["merchant_name"] == "ФОП Левинець"
    assert body["merchants"][0]["nickname"] is None


def test_merchants_endpoint_preserves_nicknames_across_refresh(
    client_with_terminal: TestClient, auth_headers: dict, fake_terminal,
) -> None:
    """Operator names "ФОП Левинець" as "Бар"; next GET /merchants
    (refreshing from SSI) must keep that nickname. New bank-side
    merchant appears with no nickname; removed merchant disappears."""
    _register_default(client_with_terminal, auth_headers, fake_terminal.id)
    # First poll — pure bank-side list.
    with patch(
        "src.services.terminals.ssi.SSITerminalAdapter.list_merchants",
        new=AsyncMock(return_value=[
            MerchantInfo("M1", "T1", "ФОП Левинець"),
            MerchantInfo("M2", "T1", "ТОВ Smile Bar"),
        ]),
    ):
        client_with_terminal.get(
            f"/terminal/{fake_terminal.id}/merchants", headers=auth_headers,
        )

    # Operator sets nicknames.
    put_response = client_with_terminal.put(
        f"/terminal/{fake_terminal.id}/merchants",
        headers=auth_headers,
        json={"merchants": [
            {"merchant_id": "M1", "terminal_id": "T1", "nickname": "Бар", "merchant_name": "ФОП Левинець"},
            {"merchant_id": "M2", "terminal_id": "T1", "nickname": "Тераса", "merchant_name": "ТОВ Smile Bar"},
        ]},
    )
    assert put_response.status_code == 200

    # Re-poll: SSI now reports M1 + a NEW M3 (M2 removed bank-side).
    with patch(
        "src.services.terminals.ssi.SSITerminalAdapter.list_merchants",
        new=AsyncMock(return_value=[
            MerchantInfo("M1", "T1", "ФОП Левинець"),
            MerchantInfo("M3", "T1", "ФОП Нове"),
        ]),
    ):
        refreshed = client_with_terminal.get(
            f"/terminal/{fake_terminal.id}/merchants", headers=auth_headers,
        ).json()

    by_id = {m["merchant_id"]: m for m in refreshed["merchants"]}
    assert set(by_id) == {"M1", "M3"}
    assert by_id["M1"]["nickname"] == "Бар"      # survived
    assert by_id["M3"]["nickname"] is None       # new — no nickname yet


def test_merchants_put_unknown_terminal_returns_404(
    client_with_terminal: TestClient, auth_headers: dict,
) -> None:
    response = client_with_terminal.put(
        "/terminal/never-registered/merchants",
        headers=auth_headers,
        json={"merchants": []},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "unknown_terminal"


def test_register_with_initial_merchants_persists_nicknames(
    client_with_terminal: TestClient, auth_headers: dict, fake_terminal,
) -> None:
    """Frontend can push the merchant list with nicknames in the
    initial /register call — same shape as the bulk PUT. Useful for
    single-merchant terminals where the UI never shows an editor and
    just defaults the nickname to the bank name."""
    client_with_terminal.post("/terminal/discover", headers=auth_headers)
    response = client_with_terminal.post(
        "/terminal/register",
        headers=auth_headers,
        json={
            "id": fake_terminal.id,
            "kind": "mono_pos",
            "default_merchant_id": "M1",
            "merchants": [
                {"merchant_id": "M1", "terminal_id": "T1", "nickname": "Бар"},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["terminal"]["merchants"][0]["nickname"] == "Бар"


def test_charge_endpoint_returns_acquirer_result(
    client_with_terminal: TestClient, auth_headers: dict, fake_terminal,
) -> None:
    _register_default(client_with_terminal, auth_headers, fake_terminal.id)
    fake_result = AcquirerResult(
        status="ok",
        rrn="9999999999",
        auth_code="123456",
        cardmask="4725XXXXXXXX1627",
        paysys="VISA",
        raw_transaction_result="APPROVED_ONLINE",
    )
    with patch(
        "src.services.terminals.ssi.SSITerminalAdapter.charge",
        new=AsyncMock(return_value=fake_result),
    ):
        response = client_with_terminal.post(
            "/terminal/charge",
            headers=auth_headers,
            json={"amount_kopecks": 24500, "transaction_uid": "u-1"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["terminal_id"] == fake_terminal.id  # fell back to first()
    assert body["result"]["status"] == "ok"
    assert body["result"]["rrn"] == "9999999999"


def test_charge_endpoint_surfaces_terminal_unavailable_as_503(
    client_with_terminal: TestClient, auth_headers: dict, fake_terminal,
) -> None:
    """Adapter raised TerminalUnavailable(code=...) → route returns 503
    with that code in detail.code so the frontend's existing 503
    handler picks it up (same shape as printer routes)."""
    _register_default(client_with_terminal, auth_headers, fake_terminal.id)
    with patch(
        "src.services.terminals.ssi.SSITerminalAdapter.charge",
        new=AsyncMock(side_effect=TerminalUnavailable("offline", code="unreachable")),
    ):
        response = client_with_terminal.post(
            "/terminal/charge",
            headers=auth_headers,
            json={"amount_kopecks": 100},
        )
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "unreachable",
        "message": "offline",
    }


def test_cancel_endpoint_always_returns_200(
    client_with_terminal: TestClient, auth_headers: dict, fake_terminal,
) -> None:
    """Cancel is best-effort by contract — the route returns 200 even
    if the underlying Interrupt errored, so the UI can always close
    its 'cancelling' modal."""
    _register_default(client_with_terminal, auth_headers, fake_terminal.id)
    with patch(
        "src.services.terminals.ssi.SSITerminalAdapter.cancel",
        new=AsyncMock(return_value=None),
    ):
        response = client_with_terminal.post(
            f"/terminal/{fake_terminal.id}/cancel", headers=auth_headers,
        )
    assert response.status_code == 200
    assert response.json()["status"] == "cancel_requested"


def test_last_result_endpoint_with_uid(
    client_with_terminal: TestClient, auth_headers: dict, fake_terminal,
) -> None:
    _register_default(client_with_terminal, auth_headers, fake_terminal.id)
    fake_result = AcquirerResult(status="ok", transaction_uid="u-42")
    with patch(
        "src.services.terminals.ssi.SSITerminalAdapter.get_last_result",
        new=AsyncMock(return_value=fake_result),
    ) as called:
        response = client_with_terminal.get(
            f"/terminal/{fake_terminal.id}/last-result?transaction_uid=u-42",
            headers=auth_headers,
        )
    assert response.status_code == 200
    assert response.json()["result"]["transaction_uid"] == "u-42"
    called.assert_called_once_with(transaction_uid="u-42")


def test_endpoints_require_api_key(client_with_terminal: TestClient) -> None:
    """No X-Api-Key → 401. Same posture as the printer routes."""
    response = client_with_terminal.post("/terminal/discover")
    assert response.status_code == 401
