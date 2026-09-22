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
    RefundRequest,
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


# ---------- повернення (BH-177) ----------
#
# Доти емулятор SSI не знав ні `Refund`, ні `Void`, ні `PartialVoid`: вони
# падали в загальний `return {"error": False}`, після якого `GetStatus`
# віддавав S00, а `GetLastResult` — APPROVED із вигаданими rrn і кодом
# авторизації, НЕ спитавши оператора. Тобто перевірка повернення на SSI
# проходила завжди, хоч би що було в адаптері.

REFUND_CASES = [
    ("ssi", SSITerminalEmulator, SSITerminalAdapter, TerminalKind.mono_pos),
    ("privat", PrivatTerminalEmulator, PrivatBankTerminalAdapter, TerminalKind.privat_pos),
]


def _run(server_emu, body, decision=None):
    """Підняти емулятор, виконати корутину проти нього, прибрати за собою."""
    decisions: "queue.Queue[Pending]" = queue.Queue()
    emulator = server_emu(decisions=decisions)
    server = EmulatorServer("127.0.0.1", 0, emulator)
    server.start()
    stop = _auto_operator(decisions, decision) if decision else None
    try:
        return asyncio.run(asyncio.wait_for(body(server.port), timeout=15)), decisions
    finally:
        if stop:
            stop.set()
        server.stop()


@pytest.mark.parametrize("name,emu_cls,adapter_cls,kind", REFUND_CASES,
                         ids=[c[0] for c in REFUND_CASES])
@pytest.mark.parametrize("decision,expected", DECISIONS, ids=[d[0] for d in DECISIONS])
def test_emulator_refund(name, emu_cls, adapter_cls, kind, decision, expected):
    """Повернення доходить до оператора, і його рішення — і є результатом."""
    async def body(port):
        adapter = adapter_cls(_reg("127.0.0.1", port, kind))
        return await adapter.refund(RefundRequest(
            amount_kopecks=12300, rrn="123456789012",
            auth_code="654321", transaction_uid="r1",
        ))

    result, _ = _run(emu_cls, body, decision)
    assert result.status == expected, f"{name}: {result.status} != {expected}"


def test_ssi_refund_asks_the_operator():
    """Головне: без рішення людини повернення не «схвалюється» саме собою."""
    async def body(port):
        adapter = SSITerminalAdapter(_reg("127.0.0.1", port, TerminalKind.mono_pos))
        with pytest.raises(asyncio.TimeoutError):
            # Оператора немає — адаптер мусить чекати, а не дістати APPROVED.
            await asyncio.wait_for(adapter.refund(RefundRequest(
                amount_kopecks=100, rrn="123456789012", transaction_uid="r2",
            )), timeout=2)
        return True

    ok, decisions = _run(SSITerminalEmulator, body)
    assert ok
    pending = decisions.get_nowait()
    assert pending.kind == "refund"
    assert "123456789012" in pending.reference


@pytest.mark.parametrize("missing,operation,message", [
    ("refund_reference", "Refund", "rrn"),
    ("invoice", "Void", "invoiceNum"),
])
def test_ssi_refund_without_its_key_is_refused(missing, operation, message):
    """Справжній термінал не проведе повернення без номерів оплати, і емулятор
    не має вдавати, що проведе. Перевірка через САМ протокол: адаптер сюди не
    пускає (він теж валідує), тож шлемо кадр так, як його шле адаптер."""
    from emulator.ssi_terminal import SSITerminalEmulator as Emu

    emu = Emu(decisions=queue.Queue())
    response = emu.handle({"method": operation, "params": {"transAmount": "100"}})

    assert response["error"] is True
    assert message in response["errorDescription"]


def test_ssi_unknown_method_is_refused_instead_of_approved():
    """Саме та вада: невідома команда давала «успіх», і далі APPROVED."""
    emu = SSITerminalEmulator(decisions=queue.Queue())

    ack = emu.handle({"method": "SomethingNew", "params": {}})

    assert ack["error"] is True
    assert emu.handle({"method": "GetStatus"})["status"] == "S00"


@pytest.mark.parametrize("name,emu_cls,request_frame,ok_field,ok_value", [
    ("ssi", SSITerminalEmulator, {"method": "Nope"}, "error", False),
    ("privat", PrivatTerminalEmulator, {"method": "Nope"}, "error", False),
    ("posapi", PosApiTerminalEmulator, {"function": "NOPE"}, "responseCode", "00"),
    ("bpos", BposTerminalEmulator, {"cmd": "777"}, "result", "0"),
    ("oschad", OschadTerminalEmulator, {"op": "nope"}, "rc", "000"),
], ids=["ssi", "privat", "posapi", "bpos", "oschad"])
def test_an_unknown_command_is_refused_by_every_emulator(
    name, emu_cls, request_frame, ok_field, ok_value,
):
    """Усі п'ять емуляторів мали в кінці `handle` загальне «успішно», тож
    будь-яка команда, якої вони не знають, виглядала виконаною. Для SSI це
    означало вигадане APPROVED на повернення; для решти — тиху неправду про
    операцію, якої не було."""
    emu = emu_cls(decisions=queue.Queue())

    response = emu.handle(request_frame)

    assert response[ok_field] != ok_value, f"{name}: невідома команда дала «успіх»"


def _handle_without_blocking(emu, frame, timeout: float = 3.0) -> dict:
    """Викликати `handle` і НЕ дати тесту зависнути, якщо той раптом почне
    чекати на рішення оператора. Саме зависання і є вадою: запит, який не
    рухає грошей, не має блокувати консоль.
    """
    box: dict = {}

    def run() -> None:
        box["response"] = emu.handle(frame)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    assert "response" in box, "handle() чекає на оператора там, де не мусить"
    return box["response"]


def test_privat_refund_tells_the_operator_it_is_a_refund():
    """Те саме, що й для SSI: у консолі має стояти ПОВЕРНЕННЯ, а не «Оплата».

    Знайдено ревʼю: механіку `kind`/`reference` додали, але шлях Привату
    лишився на спільному `_purchase`, тобто людина підтверджувала б списання.
    """
    decisions: "queue.Queue[Pending]" = queue.Queue()
    emu = PrivatTerminalEmulator(decisions=decisions)
    seen: list[Pending] = []
    # Свій «оператор»: стандартний `_auto_operator` ЗАБИРАЄ елемент із черги,
    # тож після нього перевіряти вже нічого. Тут запамʼятовуємо те, що
    # підтвердили, — саме це бачить людина в консолі.
    stop = threading.Event()

    def operator() -> None:
        while not stop.is_set():
            try:
                pending = decisions.get(timeout=0.1)
            except queue.Empty:
                continue
            seen.append(pending)
            pending.decision = "a"
            pending.event.set()

    threading.Thread(target=operator, daemon=True).start()
    try:
        response = emu.handle({
            "method": "Refund", "step": 0,
            "params": {"amount": "123.00", "rrn": "123456789012"},
        })
    finally:
        stop.set()

    assert response["method"] == "Refund"
    assert response["error"] is False
    # Головне тут — не відповідь, а те, ЩО побачив оператор: відповідь несе
    # `method="Refund"` навіть тоді, коли в консоль пішла «Оплата» (знайдено
    # другим колом ревʼю — перша версія цього тесту вади не ловила).
    assert seen, "операції взагалі не показали оператору"
    assert seen[0].kind == "refund"
    assert seen[0].label == "ПОВЕРНЕННЯ"
    assert "123456789012" in seen[0].reference


def test_privat_refund_without_rrn_is_refused_without_asking_anyone():
    emu = PrivatTerminalEmulator(decisions=queue.Queue())

    response = _handle_without_blocking(
        emu, {"method": "Refund", "step": 0, "params": {"amount": "123.00"}},
    )

    assert response["error"] is True
    assert "rrn" in response["errorDescription"]


def test_privat_receipt_lookup_does_not_ask_the_operator_to_approve_money():
    """`GetReceiptInfo` лише читає чек. Доти він ішов шляхом оплати й вішав
    консоль питанням «підтвердіть ₴0.00» (знайдено ревʼю)."""
    decisions: "queue.Queue[Pending]" = queue.Queue()
    emu = PrivatTerminalEmulator(decisions=decisions)

    response = _handle_without_blocking(
        emu,
        {"method": "GetReceiptInfo", "step": 0, "params": {"invoiceNumber": "000777"}},
    )

    assert response["error"] is False
    assert response["params"]["invoiceNumber"] == "000777"
    assert decisions.empty(), "у оператора спитали про довідку як про оплату"
