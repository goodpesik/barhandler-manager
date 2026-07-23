"""Adapter ↔ fake-bridge round-trip tests for the three new protocols.

Each protocol gets a tiny in-process asyncio server that speaks its exact
framing (newline for PosAPI, 4-byte length prefix for BPOS, STX/ETX for
Oschad) and returns canned responses. We drive the real adapter against it
and assert the unified AcquirerResult mapping — this is the contract the
bundled emulator must also satisfy.

No pytest-asyncio dependency: each test wraps its body in asyncio.run() and
starts the fake server on the same loop.
"""

from __future__ import annotations

import asyncio
import json

from src.models.terminal import (
    ChargeRequest,
    TerminalDescriptor,
    TerminalKind,
    TerminalNetworkAddress,
    TerminalRegistration,
    TerminalTransport,
)
from src.services.terminals.bpos import BposTerminalAdapter
from src.services.terminals.oschad import OschadTerminalAdapter
from src.services.terminals.posapi import PosApiTerminalAdapter


# ---------------------------------------------------------------------------
# Fake bridges — one framing + responder per protocol
# ---------------------------------------------------------------------------


def _posapi_respond(req: dict) -> dict:
    fn = req.get("function")
    if fn == "ECHO":
        return {"function": "ECHO", "responseCode": "00"}
    if fn == "GET_INFO":
        return {"function": "GET_INFO", "responseCode": "00",
                "model": "Verifone X990", "serial": "SN-POSAPI"}
    if fn == "GET_MERCHANTS":
        return {"function": "GET_MERCHANTS", "responseCode": "00",
                "merchants": [{"merchantId": "M1", "terminalId": "T1", "name": "Бар"}]}
    if fn == "PURCHASE":
        amt = req.get("amount")
        if amt == 1:  # sentinel → decline
            return {"function": "PURCHASE", "responseCode": "05", "approved": False,
                    "responseText": "Do not honour"}
        return {"function": "PURCHASE", "responseCode": "00", "approved": True,
                "rrn": "000000000123", "authCode": "654321", "pan": "**1234",
                "cardScheme": "Visa", "terminalId": "T1",
                "reference": req.get("reference")}
    if fn in ("ABORT", "VOID"):
        return {"function": fn, "responseCode": "00"}
    if fn == "GET_LAST":
        return {"function": "GET_LAST", "responseCode": "00", "approved": True,
                "rrn": "000000000123", "authCode": "654321"}
    return {"function": fn, "responseCode": "00"}


async def _posapi_handler(reader, writer):
    try:
        line = await reader.readuntil(b"\n")
        req = json.loads(line[:-1].decode())
        resp = _posapi_respond(req)
        writer.write(json.dumps(resp).encode() + b"\n")
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


def _bpos_respond(req: dict) -> dict:
    cmd = req.get("cmd")
    if cmd == "0":
        return {"cmd": "0", "result": "0"}
    if cmd == "90":
        return {"cmd": "90", "result": "0", "model": "Ingenico Move", "sn": "SN-BPOS"}
    if cmd == "80":
        return {"cmd": "80", "result": "0",
                "merchants": [{"merchantId": "MB", "name": "Тераса"}]}
    if cmd == "1":
        if req.get("amount") == "0.01":  # sentinel → cancelled
            return {"cmd": "1", "result": "4", "message": "Скасовано"}
        return {"cmd": "1", "result": "0", "rrn": "999", "auth": "111",
                "pan": "**5678", "cardName": "MasterCard", "terminalId": "TB",
                "invoice": "7"}
    if cmd == "9":
        return {"cmd": "9", "result": "0"}
    if cmd == "95":
        return {"cmd": "95", "result": "0", "rrn": "999", "auth": "111"}
    return {"cmd": cmd, "result": "0"}


async def _bpos_handler(reader, writer):
    try:
        header = await reader.readexactly(4)
        body = await reader.readexactly(int.from_bytes(header, "big"))
        req = json.loads(body.decode())
        resp = json.dumps(_bpos_respond(req)).encode()
        writer.write(len(resp).to_bytes(4, "big") + resp)
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


def _oschad_respond(req: dict) -> dict:
    op = req.get("op")
    if op == "ping":
        return {"op": "ping", "rc": "000"}
    if op == "info":
        return {"op": "info", "rc": "000", "model": "PAX A930", "sn": "SN-OSCHAD"}
    if op == "merchants":
        return {"op": "merchants", "rc": "000",
                "merchants": [{"mid": "MO", "name": "Каса"}]}
    if op == "sale":
        return {"op": "sale", "rc": "000", "rrn": "777", "auth": "222",
                "pan": "**9999", "scheme": "Visa", "tid": "TO", "invoice": "3"}
    if op == "abort":
        return {"op": "abort", "rc": "000"}
    if op == "last":
        return {"op": "last", "rc": "000", "rrn": "777", "auth": "222"}
    return {"op": op, "rc": "000"}


async def _oschad_handler(reader, writer):
    try:
        raw = await reader.readuntil(b"\x03")
        body = raw[:-1]
        if body[:1] == b"\x02":
            body = body[1:]
        req = json.loads(body.decode())
        resp = json.dumps(_oschad_respond(req)).encode()
        writer.write(b"\x02" + resp + b"\x03")
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


async def _start(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    return server, host, port


def _reg(host: str, port: int, kind: TerminalKind) -> TerminalRegistration:
    return TerminalRegistration(
        descriptor=TerminalDescriptor(
            id="t-test",
            transport=TerminalTransport.network,
            label="test",
            kind=kind,
            network=TerminalNetworkAddress(host=host, port=port),
        ),
        kind=kind,
        default_merchant_id="M1",
    )


# ---------------------------------------------------------------------------
# PosAPI (Printec) — Raiffeisen / PUMB
# ---------------------------------------------------------------------------


def test_posapi_probe_ping_merchants_and_charge():
    async def body():
        server, host, port = await _start(_posapi_handler)
        try:
            desc = await PosApiTerminalAdapter.probe(host, port)
            assert desc is not None
            assert desc.kind == TerminalKind.generic_posapi.value
            assert desc.model == "Verifone X990"

            adapter = PosApiTerminalAdapter(_reg(host, port, TerminalKind.raif_pos))
            assert await adapter.ping() is True

            merchants = await adapter.list_merchants()
            assert [m.merchant_id for m in merchants] == ["M1"]

            res = await adapter.charge(
                ChargeRequest(amount_kopecks=12300, transaction_uid="u1"))
            assert res.status == "ok"
            assert res.rrn == "000000000123"
            assert res.auth_code == "654321"
            assert res.paysys == "Visa"
            assert res.transaction_uid == "u1"

            decl = await adapter.charge(ChargeRequest(amount_kopecks=1))
            assert decl.status == "declined"
            assert decl.error_code == "05"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(body())


# ---------------------------------------------------------------------------
# BPOS — Pivdenny / Sense
# ---------------------------------------------------------------------------


def test_bpos_probe_and_charge_and_cancel():
    async def body():
        server, host, port = await _start(_bpos_handler)
        try:
            desc = await BposTerminalAdapter.probe(host, port)
            assert desc is not None
            assert desc.kind == TerminalKind.generic_bpos.value
            assert desc.serial == "SN-BPOS"

            adapter = BposTerminalAdapter(_reg(host, port, TerminalKind.pivdenny_pos))
            res = await adapter.charge(ChargeRequest(amount_kopecks=12300))
            assert res.status == "ok"
            assert res.rrn == "999"
            assert res.paysys == "MasterCard"
            assert res.invoice_num == "7"

            cancelled = await adapter.charge(ChargeRequest(amount_kopecks=1))
            assert cancelled.status == "cancelled"

            await adapter.cancel()  # must not raise
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(body())


# ---------------------------------------------------------------------------
# Oschad
# ---------------------------------------------------------------------------


def test_oschad_probe_and_charge():
    async def body():
        server, host, port = await _start(_oschad_handler)
        try:
            desc = await OschadTerminalAdapter.probe(host, port)
            assert desc is not None
            assert desc.kind == TerminalKind.oschad_pos.value
            assert desc.model == "PAX A930"

            adapter = OschadTerminalAdapter(_reg(host, port, TerminalKind.oschad_pos))
            assert await adapter.ping() is True

            res = await adapter.charge(
                ChargeRequest(amount_kopecks=5000, transaction_uid="ux"))
            assert res.status == "ok"
            assert res.rrn == "777"
            assert res.bank_name == "Oschadbank"
            assert res.terminal_id == "TO"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(body())


# ---------------------------------------------------------------------------
# A wrong-protocol server must NOT be mis-probed as another protocol
# ---------------------------------------------------------------------------


def test_posapi_probe_rejects_bpos_server():
    async def body():
        server, host, port = await _start(_bpos_handler)
        try:
            # PosAPI probe sends newline-JSON; the BPOS server expects a
            # length prefix, so the handshake must fail → None, not a crash.
            desc = await PosApiTerminalAdapter.probe(host, port)
            assert desc is None
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(body())
