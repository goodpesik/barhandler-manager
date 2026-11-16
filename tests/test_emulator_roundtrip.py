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
from typing import Optional

import pytest

from emulator.base_terminal import (
    DEFAULT_MERCHANT_ID,
    DEFAULT_MERCHANT_NAME,
    DEFAULT_TERMINAL_ID,
    EmulatorServer,
    Merchant,
    Pending,
    _step_id,
    build_merchants,
)
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


def _wire_merchant_ids(name: str, merchants: list[Merchant]) -> list[str]:
    """How each protocol addresses a merchant in its requests.

    PrivatBank names the merchant's INDEX in getMerchantList (spec 6.4);
    every other protocol carries the merchant id itself.
    """
    if name == "privat":
        return [str(index) for index in range(len(merchants))]
    return [m.merchant_id for m in merchants]


#: Protocols whose merchant-list entry carries a terminal id of its own. The
#: other three answer with the merchant id and the name only, so their
#: adapters have no terminal id to report.
PROTOCOLS_WITH_WIRE_TERMINAL_ID = frozenset({"ssi", "posapi"})


def _wire_terminal_ids(name: str, merchants: list[Merchant]) -> list[Optional[str]]:
    """The terminal id each protocol reports per merchant-list entry.

    SSI (`GetMerchantListDetailed`) and PosAPI (`GET_MERCHANTS`) carry one per
    entry; BPOS (cmd `80`), Oschad (`merchants`) and PrivatBank
    (`getMerchantList`) carry none, so the adapter reports None.
    """
    if name in PROTOCOLS_WITH_WIRE_TERMINAL_ID:
        return [m.terminal_id for m in merchants]
    return [None] * len(merchants)


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


def _run(server_emu, body, decision=None, merchant_count=1):
    """Підняти емулятор, виконати корутину проти нього, прибрати за собою."""
    decisions: "queue.Queue[Pending]" = queue.Queue()
    emulator = server_emu(decisions=decisions, merchant_count=merchant_count)
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


# ---------- two merchants on one terminal ----------


def test_the_derived_second_merchant_is_distinct_from_the_first():
    """One merchant stays exactly what it has always been, and the second is
    told apart by every field an operation can be addressed by."""
    one = build_merchants(1)
    two = build_merchants(2)

    assert len(one) == 1
    assert two[0] == one[0]
    assert two[1].merchant_id != two[0].merchant_id
    assert two[1].terminal_id != two[0].terminal_id
    assert two[1].merchant_name != two[0].merchant_name


@pytest.mark.parametrize("count", [1, 2], ids=["one", "two"])
@pytest.mark.parametrize("name,emu_cls,adapter_cls,kind", CASES,
                         ids=[c[0] for c in CASES])
def test_adapter_lists_every_merchant_the_terminal_has(
    name, emu_cls, adapter_cls, kind, count,
):
    """`list_merchants` feeds the payment window's merchant select, so each
    emulator has to report all of them in the shape its own adapter parses."""
    expected = build_merchants(count)

    async def body(port):
        adapter = adapter_cls(_reg("127.0.0.1", port, kind))
        return await adapter.list_merchants()

    merchants, _ = _run(emu_cls, body, merchant_count=count)

    assert [m.merchant_id for m in merchants] == _wire_merchant_ids(name, expected)
    assert [m.merchant_name for m in merchants] == [m.merchant_name for m in expected]
    # A terminal id reported for the wrong entry is worse than none: the
    # manager keys a stored merchant nickname on (merchant id, terminal id).
    assert [m.terminal_id for m in merchants] == _wire_terminal_ids(name, expected)


def test_one_merchant_keeps_the_documented_default_values():
    """The single-merchant terminal is what the README and the register `curl`
    describe, so the three defaults are pinned by value."""
    one = build_merchants(1)

    assert len(one) == 1
    assert one[0].merchant_id == "00000012345"
    assert one[0].merchant_name == "EMULATOR MERCHANT"
    assert one[0].terminal_id == "EMU00001"
    # And the module constants are the same values the emulator is started with.
    assert DEFAULT_MERCHANT_ID == "00000012345"
    assert DEFAULT_MERCHANT_NAME == "EMULATOR MERCHANT"
    assert DEFAULT_TERMINAL_ID == "EMU00001"


@pytest.mark.parametrize("value,position,expected", [
    # Leading zeros are part of the id width a terminal reports.
    ("00000012345", 2, "00000012346"),
    ("EMU00001", 2, "EMU00002"),
    # A carry needs one more digit than the value had.
    ("9", 2, "10"),
    ("99", 2, "100"),
    ("EMU99", 2, "EMU100"),
    # No trailing digits to step: the position is appended instead.
    ("abc", 2, "abc-2"),
    # Position 1 is the first merchant, which keeps its id as given.
    ("00000012345", 1, "00000012345"),
    ("EMU00001", 1, "EMU00001"),
])
def test_step_id_moves_the_trailing_number_and_keeps_its_width(
    value, position, expected,
):
    assert _step_id(value, position) == expected


@pytest.mark.parametrize("value", ["00000012345", "9", "99", "abc"])
def test_step_id_does_not_collide_with_the_value_it_steps_from(value):
    """Two merchants addressed by the same id are one merchant."""
    assert _step_id(value, 2) != value


def test_the_second_merchant_is_derived_from_custom_ids_without_collision():
    """Custom `--merchant-id` / `--terminal-id` with no trailing number still
    produce a second merchant told apart by every field."""
    two = build_merchants(
        2, merchant_id="abc", merchant_name="SHOP", terminal_id="tid",
    )

    assert [m.merchant_id for m in two] == ["abc", "abc-2"]
    assert [m.terminal_id for m in two] == ["tid", "tid-2"]
    assert [m.merchant_name for m in two] == ["SHOP", "SHOP 2"]
