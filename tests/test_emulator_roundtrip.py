"""End-to-end: the bundled emulator vs. the real manager adapters.

For every supported protocol we start the emulator's device-side server on an
ephemeral port and drive the actual adapter (`charge`) against it, with a
background "operator" that auto-answers the console decision queue. This is
the guarantee that the emulator stays byte-consistent with each adapter — if
a framing or field name drifts on either side, one of these fails.
"""

from __future__ import annotations

import asyncio
import queue
import threading

import pytest

from emulator.base_terminal import EmulatorServer, Pending
from emulator.bpos_terminal import BposTerminalEmulator
from emulator.oschad_terminal import OschadTerminalEmulator
from emulator.posapi_terminal import PosApiTerminalEmulator
from emulator.privat_terminal import PrivatTerminalEmulator
from emulator.ssi_terminal import SSITerminalEmulator
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
from src.services.terminals.privatbank import PrivatBankTerminalAdapter
from src.services.terminals.ssi import SSITerminalAdapter

CASES = [
    ("ssi", SSITerminalEmulator, SSITerminalAdapter, TerminalKind.mono_pos),
    ("privat", PrivatTerminalEmulator, PrivatBankTerminalAdapter, TerminalKind.privat_pos),
    ("posapi", PosApiTerminalEmulator, PosApiTerminalAdapter, TerminalKind.raif_pos),
    ("bpos", BposTerminalEmulator, BposTerminalAdapter, TerminalKind.pivdenny_pos),
    ("oschad", OschadTerminalEmulator, OschadTerminalAdapter, TerminalKind.oschad_pos),
]

DECISIONS = [("a", "ok"), ("d", "declined"), ("c", "cancelled")]


def _reg(host: str, port: int, kind: TerminalKind) -> TerminalRegistration:
    return TerminalRegistration(
        descriptor=TerminalDescriptor(
            id="emu", transport=TerminalTransport.network, label="emu", kind=kind,
            network=TerminalNetworkAddress(host=host, port=port),
        ),
        kind=kind, default_merchant_id="M1",
    )


def _auto_operator(decisions: "queue.Queue[Pending]", decision: str) -> threading.Event:
    """Background console stand-in: answer every Purchase with `decision`."""
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            try:
                pending = decisions.get(timeout=0.1)
            except queue.Empty:
                continue
            pending.decision = decision
            pending.event.set()

    threading.Thread(target=run, daemon=True).start()
    return stop


@pytest.mark.parametrize("name,emu_cls,adapter_cls,kind", CASES,
                         ids=[c[0] for c in CASES])
@pytest.mark.parametrize("decision,expected", DECISIONS, ids=[d[0] for d in DECISIONS])
def test_emulator_charge(name, emu_cls, adapter_cls, kind, decision, expected):
    decisions: "queue.Queue[Pending]" = queue.Queue()
    emulator = emu_cls(decisions=decisions)
    server = EmulatorServer("127.0.0.1", 0, emulator)
    server.start()
    stop = _auto_operator(decisions, decision)
    try:
        async def body():
            adapter = adapter_cls(_reg("127.0.0.1", server.port, kind))
            return await adapter.charge(
                ChargeRequest(amount_kopecks=12300, transaction_uid="u1"))

        result = asyncio.run(asyncio.wait_for(body(), timeout=15))
        assert result.status == expected, f"{name}: {result.status} != {expected}"
        if expected == "ok":
            assert result.rrn, f"{name}: approved result missing rrn"
    finally:
        stop.set()
        server.stop()


@pytest.mark.parametrize("name,emu_cls,adapter_cls,kind", CASES,
                         ids=[c[0] for c in CASES])
def test_emulator_ping_and_probe(name, emu_cls, adapter_cls, kind):
    decisions: "queue.Queue[Pending]" = queue.Queue()
    server = EmulatorServer("127.0.0.1", 0, emu_cls(decisions=decisions))
    server.start()
    try:
        async def body():
            desc = await adapter_cls.probe("127.0.0.1", server.port)
            adapter = adapter_cls(_reg("127.0.0.1", server.port, kind))
            alive = await adapter.ping()
            return desc, alive

        desc, alive = asyncio.run(asyncio.wait_for(body(), timeout=10))
        assert desc is not None, f"{name}: probe returned None"
        assert alive is True, f"{name}: ping failed"
    finally:
        server.stop()
