"""End-to-end: discover → register → list → unregister."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.constants import DEFAULT_API_KEY
from src.models.printer import (
    PrinterDescriptor,
    PrinterTransport,
    UsbAddress,
    make_id,
)
from src.server import create_app


@pytest.fixture
def fake_usb_printer() -> PrinterDescriptor:
    """A descriptor that looks like what `discover_usb()` returns for
    an STMicro-class 58 mm printer — tests can pin against this without
    needing real hardware."""
    return PrinterDescriptor(
        id=make_id(PrinterTransport.usb, "0456", "0808", ""),
        transport=PrinterTransport.usb,
        label="STMicro POS Printer",
        manufacturer="STMicroelectronics",
        product="USB POS Printer",
        usb=UsbAddress(vendor_id=0x0456, product_id=0x0808, in_ep=0x81, out_ep=0x03),
    )


@pytest.fixture
def client_with_usb(
    config: dict,
    fake_usb_printer: PrinterDescriptor,
):
    """Same setup as `client` but USB discovery returns one fake unit."""
    with patch("src.devices.scan.discover_usb", return_value=[fake_usb_printer]), \
         patch("src.devices.scan.discover_network", return_value=[]), \
         patch("src.devices.scan.discover_bluetooth", return_value=[]):
        app = create_app(config)
        with TestClient(app) as c:
            yield c


def test_discover_returns_fake_printer(
    client_with_usb: TestClient,
    auth_headers: dict,
    fake_usb_printer: PrinterDescriptor,
) -> None:
    response = client_with_usb.post("/devices/discover", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["printers"]) == 1
    assert body["printers"][0]["id"] == fake_usb_printer.id


def test_register_persists_to_disk(
    client_with_usb: TestClient,
    auth_headers: dict,
    fake_usb_printer: PrinterDescriptor,
    tmp_registry: Path,
) -> None:
    # Prime the registry with one discovered printer so /register knows
    # which descriptor matches the id.
    client_with_usb.post("/devices/discover", headers=auth_headers)

    register_response = client_with_usb.post(
        "/devices/register",
        headers=auth_headers,
        json={
            "id": fake_usb_printer.id,
            "kind": "receipt",
            "nickname": "Бар-чек",
            "paper_width": 58,
        },
    )
    assert register_response.status_code == 200
    reg = register_response.json()["printer"]
    assert reg["nickname"] == "Бар-чек"
    assert reg["kind"] == "receipt"

    # printers.json should now hold the registration on disk so the
    # next process boot picks it up — that's the persistence contract
    # the install / upgrade flow depends on.
    assert tmp_registry.exists()
    on_disk = json.loads(tmp_registry.read_text())
    assert on_disk["printers"][0]["descriptor"]["id"] == fake_usb_printer.id


def test_register_then_list_then_unregister(
    client_with_usb: TestClient,
    auth_headers: dict,
    fake_usb_printer: PrinterDescriptor,
) -> None:
    client_with_usb.post("/devices/discover", headers=auth_headers)
    client_with_usb.post(
        "/devices/register",
        headers=auth_headers,
        json={"id": fake_usb_printer.id, "kind": "receipt", "nickname": "Test"},
    )

    listed = client_with_usb.get("/devices", headers=auth_headers).json()
    assert len(listed["printers"]) == 1

    deleted = client_with_usb.delete(
        f"/devices/{fake_usb_printer.id}", headers=auth_headers,
    )
    assert deleted.status_code in (200, 204)

    listed_after = client_with_usb.get("/devices", headers=auth_headers).json()
    assert listed_after["printers"] == []


def test_register_unknown_printer_fails(
    client_with_usb: TestClient, auth_headers: dict,
) -> None:
    # Calling register without a prior discover (or with a totally
    # made-up id) must 404, not silently invent a descriptor.
    response = client_with_usb.post(
        "/devices/register",
        headers=auth_headers,
        json={"id": "deadbeef", "kind": "receipt"},
    )
    assert response.status_code == 404
