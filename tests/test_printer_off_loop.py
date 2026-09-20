"""BH-168 — друк більше не спиняє цикл подій.

Доти запис у принтер виконувався ПРЯМО в циклі подій: `_raw()` у python-escpos
це `self.device.write(self.out_ep, msg, self.timeout)`, а USB-ручка
створювалась без `timeout`, тобто «ждати вічно». Поки принтер жував чек — або
поки він висів — процес не робив більше НІЧОГО. Це не про друк:

  * той самий цикл веде SSI-обмін із платіжним терміналом, тож оплата
    карткою зависала разом із друком;
  * власні стелі (`STATUS_POLL_MAX_S = 180`) зроблені через `asyncio.wait_for`,
    а йому потрібен живий цикл — «оплата обмежена трьома хвилинами» переставало
    бути правдою саме тоді, коли це найважливіше;
  * `GET /busy` не відповідало — а з BH-164 його питають три інсталятори перед
    тим, як убити менеджер, і вінда-інсталятор трактує «не відповів» як «можна
    ставити».

Тести нижче падають на коді до BH-168.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

import pytest

from src.devices import printer as printer_module
from src.devices.printer import PrinterDevice


class _BlockingEscpos:
    """Принтер, який «жує» чек: `_raw` стоїть на ворітцях.

    Так виглядає будь-яке зависання заліза — від скінченого паперу на моделі
    без реального статусу до напіввисмикнутого кабелю.
    """

    def __init__(self, gate: threading.Event, safety_s: float = 10.0) -> None:
        self.gate = gate
        self.safety_s = safety_s
        self.raw: list[bytes] = []
        self.entered = threading.Event()

    def _raw(self, data) -> None:
        self.raw.append(bytes(data))
        self.entered.set()
        # Ворітця з запобіжником: тест, що забув відкрити, мусить упасти, а не
        # висіти вічно.
        self.gate.wait(self.safety_s)

    def cut(self, *_a, **_k) -> None:
        pass

    def set(self, **_k) -> None:
        pass

    def text(self, s) -> None:
        self._raw(str(s).encode("utf-8", errors="replace"))

    def close(self) -> None:
        pass


def _device(**cfg) -> PrinterDevice:
    base = {"enabled": True, "paper_width": 80, "render_mode": "native", "code_page": None}
    base.update(cfg)
    return PrinterDevice("t", base)


# ---------- цикл подій живий під час друку ----------

@pytest.mark.asyncio
async def test_the_event_loop_keeps_running_while_the_printer_blocks() -> None:
    """Головне твердження тікета. Поки запис висить, цикл мусить крутитись.

    Перевіряємо не «швидкість», а саме ЖИТТЯ циклу: доки принтер тримає байти,
    інша задача циклу мусить виконатись. На коді до BH-168 вона не виконається
    ніколи — і саме тому ворітця тут відкриває ВОНА. Тобто тест не «чекає й
    сподівається»: без фіксу настає взаємне блокування, яке ловить `wait_for`.
    """
    gate = threading.Event()
    fake = _BlockingEscpos(gate)
    dev = _device()
    dev._printer = fake
    ticks = 0

    async def _loop_is_alive() -> None:
        nonlocal ticks
        # Дочекатись, поки запис справді почався, і аж тоді відпустити.
        while not fake.entered.is_set():
            ticks += 1
            await asyncio.sleep(0.01)
        gate.set()

    async def _job(esc) -> None:
        esc._raw(b"RECEIPT")

    dev._worker_task = asyncio.create_task(dev._worker())
    alive = asyncio.create_task(_loop_is_alive())
    try:
        await asyncio.wait_for(dev.enqueue(_job), timeout=5)
    finally:
        gate.set()
        alive.cancel()
        dev._worker_task.cancel()

    assert ticks > 0, "цикл подій не зробив жодного кроку, поки принтер писав"
    assert fake.raw == [b"RECEIPT"]


@pytest.mark.asyncio
async def test_a_hung_printer_does_not_stop_other_awaits() -> None:
    """Те саме з боку сусіда: власні таймаути мусять і далі спрацьовувати.

    `asyncio.wait_for` — це те, чим обмежена оплата карткою
    (`STATUS_POLL_MAX_S` у `src/services/terminals/ssi.py`). Поки принтер
    висить, він мусить спрацювати за своїм часом, а не після друку.
    """
    gate = threading.Event()
    fake = _BlockingEscpos(gate)
    dev = _device()
    dev._printer = fake
    dev._worker_task = asyncio.create_task(dev._worker())

    async def _job(esc) -> None:
        esc._raw(b"LONG")

    printing = asyncio.create_task(dev.enqueue(_job))
    try:
        await asyncio.wait_for(asyncio.to_thread(fake.entered.wait, 5), timeout=5)
        with pytest.raises(asyncio.TimeoutError):
            # Сусідній таймаут на 50 мс мусить спрацювати, поки принтер висить.
            await asyncio.wait_for(asyncio.Event().wait(), timeout=0.05)
    finally:
        gate.set()
        await asyncio.wait_for(printing, timeout=5)
        dev._worker_task.cancel()


# ---------- байти складаються наперед, і рівно ті самі ----------

@pytest.mark.asyncio
async def test_nothing_reaches_the_printer_until_the_job_finished() -> None:
    """Джоб упав — на папір не пішло нічого.

    Доти байти летіли в залізо по ходу рендеру, тож джоб, що впав посередині,
    лишав надрукованим пів-чека. Тепер вони складаються в памʼять і зливаються
    одним заходом.
    """
    dev = _device()
    gate = threading.Event()
    gate.set()  # нічого не блокуємо — цікавить сам факт зливу
    fake = _BlockingEscpos(gate)
    dev._printer = fake
    dev._worker_task = asyncio.create_task(dev._worker())

    async def _half(esc) -> None:
        esc._raw(b"HALF")
        raise RuntimeError("поламалось посеред чека")

    try:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(dev.enqueue(_half), timeout=5)
    finally:
        dev._worker_task.cancel()

    assert fake.raw == [], "напівчек поїхав на папір"


@pytest.mark.asyncio
async def test_the_bytes_go_out_in_the_same_pieces_and_order() -> None:
    """Складання наперед не змінює того, що бачить принтер.

    Це навмисно: злити один величезний кусок замість кількох — зміна на дроті,
    яку на живому залізі я не перевіряв. Тому куски зберігаються як були.
    """
    dev = _device()
    gate = threading.Event()
    gate.set()
    fake = _BlockingEscpos(gate)
    dev._printer = fake
    dev._worker_task = asyncio.create_task(dev._worker())

    async def _three(esc) -> None:
        esc._raw(b"AAA")
        esc._raw(b"BB")
        esc._raw(b"C")

    try:
        await asyncio.wait_for(dev.enqueue(_three), timeout=5)
    finally:
        dev._worker_task.cancel()

    assert fake.raw == [b"AAA", b"BB", b"C"]


@pytest.mark.asyncio
async def test_the_printer_is_writable_again_after_a_failed_job() -> None:
    """Перехоплення `_raw` мусить зніматись НАВІТЬ коли джоб упав.

    Інакше наступний чек писав би у список, якого вже ніхто не зливає, —
    і зникав би безслідно.
    """
    dev = _device()
    gate = threading.Event()
    gate.set()
    fake = _BlockingEscpos(gate)
    dev._printer = fake
    dev._worker_task = asyncio.create_task(dev._worker())

    async def _boom(_esc) -> None:
        raise RuntimeError("boom")

    async def _good(esc) -> None:
        esc._raw(b"NEXT")

    try:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(dev.enqueue(_boom), timeout=5)
        await asyncio.wait_for(dev.enqueue(_good), timeout=5)
    finally:
        dev._worker_task.cancel()

    assert fake.raw == [b"NEXT"]
    assert "_raw" not in fake.__dict__, "латка лишилась на пристрої"


# ---------- стелі на роботу з залізом ----------

def test_usb_handle_gets_an_explicit_timeout() -> None:
    """`Usb(...)` без `timeout` — це `write(..., 0)`, тобто «ждати вічно».

    Саме через це зависання принтера ставало зависанням менеджера НАЗАВЖДИ,
    а не на кілька секунд.
    """
    dev = _device(connection="usb", vendor_id="0x0456", product_id="0x0808",
                  in_ep="0x81", out_ep="0x03")
    with patch.object(printer_module, "Usb") as usb:
        dev._build_printer()
    kwargs = usb.call_args.kwargs
    assert kwargs["timeout"] == printer_module._USB_TIMEOUT_MS
    assert kwargs["timeout"] > 0


def test_usb_timeout_can_be_raised_for_a_slow_printer() -> None:
    """Стеля велика, але не священна: дуже повільне залізо має спосіб її
    підняти, не правлячи код."""
    dev = _device(connection="usb", vendor_id="0x0456", product_id="0x0808",
                  in_ep="0x81", out_ep="0x03", usb_timeout_ms=120_000)
    with patch.object(printer_module, "Usb") as usb:
        dev._build_printer()
    assert usb.call_args.kwargs["timeout"] == 120_000


def test_network_handle_gets_an_explicit_timeout() -> None:
    dev = _device(connection="network", host="10.0.0.9", port=9100)
    with patch.object(printer_module, "Network") as net:
        dev._build_printer()
    assert net.call_args.kwargs["timeout"] == printer_module._NETWORK_TIMEOUT_S


@pytest.mark.asyncio
async def test_opening_a_hung_handle_neither_blocks_nor_lasts_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Відкриття ручки теж блокує — і його кличе вартовий кожні 15 с.

    `timeout` передач на відкриття не діє (він про самі передачі), тож стеля
    тут окрема. Без неї зависле відкриття спиняло б цикл подій регулярно й
    без жодного друку.
    """
    monkeypatch.setattr(printer_module, "_CONNECT_TIMEOUT_S", 0.2)
    dev = _device(connection="usb")
    ticks = 0

    def _hangs():
        time.sleep(3)
        raise AssertionError("це відкриття не мало дочекатись")

    monkeypatch.setattr(dev, "_build_printer", _hangs)

    async def _tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(_tick())
    try:
        assert await dev.connect() is False
    finally:
        ticker.cancel()

    assert ticks > 0, "цикл подій стояв, поки відкривалась ручка"
    assert dev._printer is None


class _RecordingEscpos(_BlockingEscpos):
    """Той самий «жувальний» принтер, що ще й записує, КОЛИ його закрили."""

    def __init__(self, gate: threading.Event) -> None:
        super().__init__(gate)
        self.events: list[str] = []
        self.writing = False
        self.closed_while_writing = False

    def _raw(self, data) -> None:
        self.writing = True
        self.events.append("write-start")
        try:
            super()._raw(data)
        finally:
            self.events.append("write-end")
            self.writing = False

    def close(self) -> None:
        if self.writing:
            self.closed_while_writing = True
        self.events.append("close")


async def _start_printing(dev: PrinterDevice, fake: _RecordingEscpos) -> asyncio.Task:
    """Покласти джоб і дочекатись, поки запис СПРАВДІ почався (передумова)."""

    async def _job(esc) -> None:
        esc._raw(b"RECEIPT")

    dev._worker_task = asyncio.create_task(dev._worker())
    printing = asyncio.create_task(dev.enqueue(_job))
    started = await asyncio.to_thread(fake.entered.wait, 5)
    assert started, "передумова тесту не склалась: запис не почався"
    assert fake.writing
    return printing


@pytest.mark.asyncio
async def test_disconnect_never_closes_the_handle_in_the_middle_of_a_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Знайдено ревʼю BH-168: `disconnect()` закривав ручку посеред запису.

    Потік, запущений через `to_thread`, скасувати неможливо — скасовується
    лише очікування. Перша версія не дочікувалась замка за 5 с і закривала
    ручку «все одно», тобто просто посеред запису, що тривав у «скасованому»
    потоці. `disconnect()` кличуть на КОЖНІЙ перереєстрації принтера, а великий
    чек законно пише десятки секунд — це не рідкісний випадок.

    Тут запис справжній і триває довше, ніж `disconnect()` згоден чекати.
    """
    monkeypatch.setattr(printer_module, "_CLOSE_WAIT_S", 0.2)
    gate = threading.Event()
    fake = _RecordingEscpos(gate)
    dev = _device()
    dev._printer = fake
    printing = await _start_printing(dev, fake)
    try:
        # Цикл подій не мусить стояти: disconnect повертається за свою стелю.
        await asyncio.wait_for(dev.disconnect(), timeout=3)
        assert "close" not in fake.events, "ручку закрили, поки запис тривав"
    finally:
        gate.set()
    # Запис доходить до кінця, і лише ПІСЛЯ нього — закриття.
    for _ in range(300):
        if "close" in fake.events:
            break
        await asyncio.sleep(0.01)
    assert fake.events == ["write-start", "write-end", "close"]
    assert not fake.closed_while_writing
    await asyncio.wait_for(printing, timeout=3)


@pytest.mark.asyncio
async def test_a_receipt_that_still_prints_after_disconnect_is_not_reported_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Знайдено ревʼю: касир чув «друк перервано», а чек тим часом виходив.

    Запис у потоці пристрою не скасовується — він доїде сам. Тож відповідь на
    запит друку мусить дати САМ запис, а не факт скасування воркера.
    """
    monkeypatch.setattr(printer_module, "_CLOSE_WAIT_S", 0.1)
    gate = threading.Event()
    fake = _RecordingEscpos(gate)
    dev = _device()
    dev._printer = fake
    printing = await _start_printing(dev, fake)
    await asyncio.wait_for(dev.disconnect(), timeout=3)
    assert not printing.done(), "відповідь дали раніше, ніж запис скінчився"
    gate.set()

    await asyncio.wait_for(printing, timeout=3)  # без винятку: чек вийшов
    assert fake.raw == [b"RECEIPT"]


@pytest.mark.asyncio
async def test_busy_stays_true_until_a_write_that_outlived_disconnect_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Друге коло ревʼю BH-168: після `disconnect()` посеред запису прапорець
    «джоб у польоті» гас одразу, хоча принтер ще писав. `/busy` казав «вільно»,
    і інсталятор убив би процес посеред чека — рівно сценарій BH-164."""
    monkeypatch.setattr(printer_module, "_CLOSE_WAIT_S", 0.1)
    gate = threading.Event()
    fake = _RecordingEscpos(gate)
    dev = _device()
    dev._printer = fake
    printing = await _start_printing(dev, fake)
    assert dev.pending_jobs() == 1

    await asyncio.wait_for(dev.disconnect(), timeout=3)
    assert fake.writing, "передумова: запис іще триває"
    assert dev.pending_jobs() == 1, "запис іще йде, а пристрій каже «вільно»"

    gate.set()
    await asyncio.wait_for(printing, timeout=3)
    for _ in range(300):
        if dev.pending_jobs() == 0:
            break
        await asyncio.sleep(0.01)
    assert dev.pending_jobs() == 0


@pytest.mark.asyncio
async def test_a_reregistered_printer_that_is_still_writing_keeps_busy_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Реєстр викидає пристрій із кешу ДО того, як `disconnect()` почався. Перевірка
    `/busy` дивилась лише в кеш — і пристрій, що дописує чек, для неї зникав."""
    from types import SimpleNamespace

    from src.devices.registry import PrinterRegistry
    from src.services.busy import printers_with_pending_jobs

    monkeypatch.setattr(printer_module, "_CLOSE_WAIT_S", 0.1)
    registry = PrinterRegistry(path=tmp_path / "printers.json")
    gate = threading.Event()
    fake = _RecordingEscpos(gate)
    dev = _device()
    dev._printer = fake
    registry._devices["p1"] = dev
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(registry=registry)))

    printing = await _start_printing(dev, fake)
    registry._drop_cached_device("p1")  # те, що робить перереєстрація
    await asyncio.sleep(0.2)  # disconnect() відпрацював свою стелю

    assert "p1" not in registry._devices
    assert printers_with_pending_jobs(request) == ["t"]

    gate.set()
    await asyncio.wait_for(printing, timeout=3)
    for _ in range(300):
        if not printers_with_pending_jobs(request):
            break
        await asyncio.sleep(0.01)
    assert printers_with_pending_jobs(request) == []


@pytest.mark.asyncio
async def test_a_late_handle_is_closed_off_the_loop_even_after_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Друге коло ревʼю: якщо `disconnect()` прибрав виконавець, поки відкриття
    ще висіло, пізня ручка закривалась ПРЯМО в циклі подій (колбек виконується
    там). `close()` завислого USB спинив би оплату."""
    monkeypatch.setattr(printer_module, "_CONNECT_TIMEOUT_S", 0.1)
    dev = _device(connection="usb")
    late = _RecordingEscpos(threading.Event())
    closed_on: list[str] = []
    late.close = lambda: closed_on.append(threading.current_thread().name)
    slow = threading.Event()

    def _slow_build():
        slow.wait(5)
        return late

    monkeypatch.setattr(dev, "_build_printer", _slow_build)
    assert await dev.connect() is False
    await dev.disconnect()  # виконавець прибрано, відкриття ще висить
    assert dev._executor is None
    slow.set()
    for _ in range(300):
        if closed_on:
            break
        await asyncio.sleep(0.01)

    assert closed_on, "пізню ручку так і не закрили"
    assert closed_on[0] != threading.current_thread().name, "закрили в циклі подій"


@pytest.mark.asyncio
async def test_disconnect_during_an_open_leaves_no_live_handle_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Третє коло ревʼю BH-168: реєстр кличе `disconnect()` на перереєстрації
    саме тоді, коли вартовий відкриває ручку. `disconnect()` проходив повз,
    а `connect()` потім ставив ручку й воркер на вже викинутому пристрої —
    захоплений USB, який не закриє ніхто."""
    dev = _device(connection="usb")
    opened = _RecordingEscpos(threading.Event())
    release = threading.Event()
    started = threading.Event()

    def _build():
        started.set()
        release.wait(5)
        return opened

    monkeypatch.setattr(dev, "_build_printer", _build)
    connecting = asyncio.create_task(dev.connect())
    assert await asyncio.to_thread(started.wait, 5), "передумова: відкриття не почалось"

    disconnecting = asyncio.create_task(dev.disconnect())
    await asyncio.sleep(0.05)  # disconnect() уже чекає на замок
    release.set()
    await asyncio.wait_for(asyncio.gather(connecting, disconnecting), timeout=5)

    for _ in range(300):
        if "close" in opened.events:
            break
        await asyncio.sleep(0.01)
    assert opened.events == ["close"], "відкриту ручку так ніхто й не закрив"
    assert dev._printer is None
    assert dev._worker_task is None or dev._worker_task.done()


@pytest.mark.asyncio
async def test_a_retired_device_does_not_open_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Після `disconnect()` пристрій списано: реєстр до нього не повернеться, тож
    відкрита на ньому ручка була б нічия."""
    dev = _device(connection="usb")
    builds = 0

    def _build():
        nonlocal builds
        builds += 1
        return _RecordingEscpos(threading.Event())

    monkeypatch.setattr(dev, "_build_printer", _build)
    await dev.disconnect()

    assert await dev.connect() is False
    assert builds == 0


@pytest.mark.asyncio
async def test_two_concurrent_connects_open_one_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Знайдено ревʼю: `connect()` отримав `await`, і вартовий разом із запитом
    на друк відкривали дві ручки; одна губилась незакритою."""
    dev = _device(connection="usb")
    builds = 0
    release = threading.Event()

    def _build():
        nonlocal builds
        builds += 1
        release.wait(2)
        return _RecordingEscpos(threading.Event())

    monkeypatch.setattr(dev, "_build_printer", _build)

    first = asyncio.create_task(dev.connect())
    second = asyncio.create_task(dev.connect())
    await asyncio.sleep(0.05)  # обидва вже всередині connect()
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    assert results == [True, True]
    assert builds == 1, f"відкрито {builds} ручок замість однієї"
    await dev.disconnect()


@pytest.mark.asyncio
async def test_a_hung_device_neither_piles_up_opens_nor_starves_the_shared_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Знайдено ревʼю: завислий `Usb(...)` займав потік СПІЛЬНОГО пулу назавжди,
    а вартовий пробує кожні 15 с. За кілька хвилин пул вичерпувався — і ставали
    фіскальні звіти й пошук терміналів, тобто весь менеджер."""
    monkeypatch.setattr(printer_module, "_CONNECT_TIMEOUT_S", 0.1)
    dev = _device(connection="usb")
    builds = 0
    unhang = threading.Event()

    def _hangs():
        nonlocal builds
        builds += 1
        unhang.wait(5)
        raise OSError("USB так і не відповів")

    monkeypatch.setattr(dev, "_build_printer", _hangs)
    try:
        for _ in range(5):  # п'ять тиків вартового
            assert await dev.connect() is False
        # Потік пристрою один, тож зайві відкриття не стартують, а СТАЮТЬ У
        # ЧЕРГУ за завислим. Лічильник у цю мить однаковий з ними й без них —
        # дивимось на саму чергу.
        assert dev._executor._work_queue.qsize() == 0, "за завислим відкриттям ставили ще й ще"
        assert builds == 1

        # Спільний пул вільний: те, чим живуть фіскальні маршрути, виконується.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 42), timeout=1) == 42
    finally:
        unhang.set()
        await dev.disconnect()


@pytest.mark.asyncio
async def test_a_handle_that_opens_after_connect_gave_up_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Відкриття, яке не вклалось у стелю, може ще повернутись. Тоді це
    захоплений USB, про який ніхто не знає, — його треба закрити."""
    monkeypatch.setattr(printer_module, "_CONNECT_TIMEOUT_S", 0.1)
    dev = _device(connection="usb")
    late = _RecordingEscpos(threading.Event())
    slow = threading.Event()

    def _slow_build():
        slow.wait(5)
        return late

    monkeypatch.setattr(dev, "_build_printer", _slow_build)
    assert await dev.connect() is False
    slow.set()
    for _ in range(300):
        if "close" in late.events:
            break
        await asyncio.sleep(0.01)
    assert late.events == ["close"]
    assert dev._printer is None
    await dev.disconnect()


# ---------- те, як це перевіряє тікет: /health і /busy під час друку ----------

def test_health_and_busy_answer_while_a_receipt_is_printing(
    config: dict, auth_headers: dict,
) -> None:
    """Перевірка з тікета, і вона про BH-164, не про зручність.

    `GET /busy` питають три інсталятори БЕЗПОСЕРЕДНЬО перед тим, як убити
    менеджер. Вінда-інсталятор трактує «не відповів» як «можна ставити» — тобто
    поки друк тримав цикл подій, оновлення могло вбити процес саме посеред
    роботи, і жодна охорона з BH-164 цього не бачила.

    Тут справжній застосунок, справжній `POST /print/text` і справжні
    `GET /health` / `GET /busy` — усі три в різних потоках, як у житті.
    """
    from fastapi.testclient import TestClient

    from src.models.printer import PrinterDescriptor, PrinterTransport, UsbAddress, make_id
    from src.server import create_app

    descriptor = PrinterDescriptor(
        id=make_id(PrinterTransport.usb, "0456", "0808", ""),
        transport=PrinterTransport.usb,
        label="STMicro POS Printer",
        usb=UsbAddress(vendor_id=0x0456, product_id=0x0808, in_ep=0x81, out_ep=0x03),
    )
    gate = threading.Event()
    fake = _BlockingEscpos(gate)

    with patch("src.devices.scan.discover_usb", return_value=[descriptor]),          patch("src.devices.scan.discover_network", return_value=[]),          patch("src.devices.scan.discover_bluetooth", return_value=[]),          patch.object(PrinterDevice, "_build_printer", lambda _self: fake):
        app = create_app(config)
        with TestClient(app) as client:
            client.post("/devices/discover", headers=auth_headers)
            registered = client.post(
                "/devices/register",
                headers=auth_headers,
                json={"id": descriptor.id, "kind": "receipt", "render_mode": "native"},
            )
            assert registered.status_code == 200, registered.text

            answers: dict = {}

            def _print() -> None:
                answers["print"] = client.post(
                    "/print/text", headers=auth_headers, json={"text": "чек"},
                ).status_code

            def _ask(name: str, path: str) -> None:
                answers[name] = client.get(path)

            printing = threading.Thread(target=_print)
            printing.start()
            try:
                assert fake.entered.wait(5), "друк так і не дійшов до заліза"
                # Обидва запити — у власних потоках: якби цикл подій стояв,
                # вони б не повернулись НІКОЛИ, і тест мусить упасти, а не
                # висіти.
                askers = [
                    threading.Thread(target=_ask, args=("health", "/health")),
                    threading.Thread(target=_ask, args=("busy", "/busy")),
                ]
                for thread in askers:
                    thread.start()
                for thread in askers:
                    thread.join(timeout=5)
                    assert not thread.is_alive(), "запит не відповів під час друку"
            finally:
                gate.set()
                printing.join(timeout=10)

            assert answers["print"] == 200
            assert answers["health"].status_code == 200
            assert answers["health"].json()["printers"][0]["status"] == "connected"
            busy = answers["busy"].json()
            assert busy["busy"] is True, "друк іде, а /busy каже «вільно»"
            assert "друк" in busy["message"]
