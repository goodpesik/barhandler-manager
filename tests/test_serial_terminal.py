"""Serial/COM transport for USB-connected terminals (PrivatBank over USB).

On Windows a USB terminal is a virtual COM port speaking the SAME PB ECR JSON.
pyserial isn't installed in the dev venv, so we inject a fake `serial` module
whose Serial echoes canned PB responses — enough to exercise the framing, the
adapter's transport routing, and the register-serial route.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from fastapi.testclient import TestClient

from src.constants import DEFAULT_API_KEY
from src.models.terminal import (
    ChargeRequest,
    TerminalDescriptor,
    TerminalKind,
    TerminalRegistration,
    TerminalSerialAddress,
    TerminalTransport,
)
from src.services.terminals import privatbank
from src.services.terminals.privatbank import (
    PrivatBankTerminalAdapter,
    encode_frame,
    decode_frame,
    send_serial,
)


def _install_fake_serial(monkeypatch, responder):
    """Inject a fake `serial` module. `responder(request_dict) -> response_dict`
    builds the reply; the fake Serial streams it back one byte per read()."""
    mod = types.ModuleType("serial")
    mod.SerialException = type("SerialException", (Exception,), {})

    class _FakeSerial:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._resp = b""
            self._pos = 0

        def reset_input_buffer(self):
            pass

        def reset_output_buffer(self):
            pass

        def write(self, data):
            req = decode_frame(bytes(data))
            self._resp = encode_frame(responder(req))
            self._pos = 0

        def flush(self):
            pass

        def read(self, n=1):
            if self._pos >= len(self._resp):
                return b""  # -> timeout in send_serial
            b = self._resp[self._pos : self._pos + 1]
            self._pos += 1
            return b

        def close(self):
            pass

    mod.Serial = _FakeSerial
    monkeypatch.setitem(sys.modules, "serial", mod)


def test_send_serial_roundtrip(monkeypatch):
    _install_fake_serial(
        monkeypatch,
        lambda req: {"method": req.get("method"), "error": False, "params": {"ok": 1}},
    )
    com = TerminalSerialAddress(port="COM4", baudrate=115200)
    resp = asyncio.run(send_serial(com, {"method": "PingDevice", "step": 0}))
    assert resp["method"] == "PingDevice"
    assert resp["error"] is False


def _reg_com(port="COM4") -> TerminalRegistration:
    desc = TerminalDescriptor(
        id="s1", transport=TerminalTransport.serial, label="COM4",
        kind=TerminalKind.privat_pos,
        com=TerminalSerialAddress(port=port, baudrate=115200),
    )
    return TerminalRegistration(descriptor=desc, kind=TerminalKind.privat_pos,
                                default_merchant_id="0")


def test_privat_adapter_charges_over_serial(monkeypatch):
    def responder(req):
        if req.get("method") == "Purchase":
            return {"method": "Purchase", "error": False, "params": {
                "trnStatus": "1", "responseCode": "0000",
                "rrn": "123456789012", "approvalCode": "654321",
                "pan": "**1234", "paymentSystem": "Visa",
            }}
        return {"method": req.get("method"), "error": False, "params": {}}

    _install_fake_serial(monkeypatch, responder)
    adapter = PrivatBankTerminalAdapter(_reg_com())
    result = asyncio.run(adapter.charge(ChargeRequest(amount_kopecks=12300)))
    assert result.status == "ok"
    assert result.rrn == "123456789012"
    assert result.paysys == "Visa"


def test_register_serial_route(client, auth_headers):
    resp = client.post("/terminal/register-serial", headers=auth_headers, json={
        "port": "COM4", "baudrate": 115200, "kind": "privat_pos",
        "nickname": "Каса USB",
    })
    assert resp.status_code == 200, resp.text
    term = resp.json()["terminal"]
    assert term["descriptor"]["transport"] == "serial"
    assert term["descriptor"]["com"]["port"] == "COM4"
    assert term["kind"] == "privat_pos"

    listed = client.get("/terminal", headers=auth_headers).json()["terminals"]
    assert any(t["descriptor"]["com"] and t["descriptor"]["com"]["port"] == "COM4"
               for t in listed)


def test_serial_scan_route_ok_without_pyserial(client, auth_headers, monkeypatch):
    # No pyserial in the dev venv → clean empty list, never 500.
    monkeypatch.setitem(sys.modules, "serial", None)
    resp = client.post("/terminal/serial-scan", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"ports": []}
