import asyncio
import logging

import pytest

from src.services.log_uplink import SocketIOLogHandler, MAX_LINE_BYTES


class FakeClient:
    def __init__(self, connected=True):
        self.connected = connected
        self.sent = []

    def emit(self, event, data, requeue=None):
        # requeue — шлях повернути запис у буфер, якщо відправка зірветься.
        # Заглушка його не викликає: тут відправка «вдається» одразу.
        self.sent.append((event, data))


def _record(msg="hello", level=logging.INFO, name="test"):
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )


def test_emits_when_connected():
    client = FakeClient(connected=True)
    h = SocketIOLogHandler(client)
    h.emit(_record("hello"))
    assert len(client.sent) == 1
    assert client.sent[0][0] == "log"
    assert client.sent[0][1]["msg"] == "hello"
    assert client.sent[0][1]["level"] == "INFO"
    assert client.sent[0][1]["logger"] == "test"


def test_buffers_when_disconnected():
    client = FakeClient(connected=False)
    h = SocketIOLogHandler(client)
    h.emit(_record("first"))
    h.emit(_record("second"))
    assert len(h._buffer) == 2
    assert client.sent == []


def test_flushes_buffer_on_reconnect():
    client = FakeClient(connected=False)
    h = SocketIOLogHandler(client)
    h.emit(_record("buffered"))
    client.connected = True
    h.flush_buffer()
    assert client.sent[0][0] == "log"
    assert client.sent[0][1]["msg"] == "buffered"
    assert len(h._buffer) == 0


def test_drops_oldest_on_overflow():
    client = FakeClient(connected=False)
    h = SocketIOLogHandler(client, buffer_limit=3)
    for i in range(5):
        h.emit(_record(f"m{i}"))
    msgs = [r.msg for r in h._buffer]
    assert msgs == ["m2", "m3", "m4"]


def test_truncates_oversize_message():
    client = FakeClient(connected=True)
    h = SocketIOLogHandler(client)
    long = "x" * (MAX_LINE_BYTES + 100)
    h.emit(_record(long))
    sent_msg = client.sent[0][1]["msg"]
    assert sent_msg.endswith("... [truncated]")
    # 4096 bytes worth of x's + suffix
    assert len(sent_msg) <= MAX_LINE_BYTES + len("... [truncated]")


# --- BH-152: обрив зʼєднання не має лити трейсбеки в лог ---------------------
#
# У клієнта (Android) uplink перепідключався кожні 2–5 хвилин, і щоразу в лог
# ішло `BadNamespaceError: /managers is not a connected namespace` разом із
# `Task exception was never retrieved`. Причина: відправка йшла через
# `loop.create_task(sio.emit(...))` без нагляду, а перевірявся `connected`
# КЛІЄНТА, не готовність неймспейса.


class _NamespaceDropClient:
    """AsyncClient-заглушка: «підключений», але неймспейс уже відвалився."""

    def __init__(self, namespaces=None, fail=True):
        self.connected = True
        self.namespaces = namespaces if namespaces is not None else {}
        self._fail = fail
        self.sent = []

    async def emit(self, event, data, namespace=None):
        if self._fail:
            raise RuntimeError("/managers is not a connected namespace.")
        self.sent.append((event, data))

    async def disconnect(self):
        self.connected = False


def _client_with(sio):
    from src.services.log_uplink import LogUplinkClient
    c = LogUplinkClient.__new__(LogUplinkClient)
    c._sio = sio
    c._cfg = {"url": "https://example.invalid"}
    c._log = logging.getLogger("uplink-test")
    c._handler = None
    c._install_id = "i"
    c._version = "0"
    c._tasks = set()
    c._diagnostics_cb = None
    return c


def test_connected_requires_the_namespace_not_just_the_transport():
    c = _client_with(_NamespaceDropClient(namespaces={}))
    assert c.connected is False, "неймспейс не піднятий — відправляти нікуди"

    c2 = _client_with(_NamespaceDropClient(namespaces={"/managers": "sid"}))
    assert c2.connected is True


def test_emit_during_a_drop_leaves_no_unretrieved_exception():
    """Виняток із відправки не має доходити до обробника циклу.

    Саме це й лило в лог `Task exception was never retrieved` з трейсбеком.
    Перша версія цього тесту була фікцією: вона перевіряла лише, що набір
    задач спорожнів — а `add_done_callback` спорожнює його й після падіння.
    Знайдено ревʼю, яке відтворило ваду й показало, що тест її не бачить.

    Тепер ставимо власний обробник винятків циклу — це той самий шлях, яким
    asyncio друкує ту помилку, — і вимагаємо, щоб він не спрацював.
    """
    import asyncio

    sio = _NamespaceDropClient(namespaces={"/managers": "sid"}, fail=True)
    c = _client_with(sio)
    caught = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: caught.append(context)
        )
        c.emit("log", {"msg": "hello"})
        # Даємо задачі виконатись і збираємо її — саме на збиранні asyncio
        # і звітує про незатребуваний виняток.
        for _ in range(5):
            await asyncio.sleep(0)
        import gc
        gc.collect()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert not caught, f"виняток дійшов до циклу: {caught}"


def test_failed_log_emit_goes_back_to_the_buffer():
    """Зірвана відправка логу мусить лишити запис у буфері.

    Знайдено ревʼю: найцінніший рядок — «uplink disconnected» — губився
    ЗАВЖДИ. Його пишуть у момент, коли неймспейс ще числиться живим, тож у
    буфер він не потрапляв, а відправка вже не вдавалась.
    """
    import asyncio

    sio = _NamespaceDropClient(namespaces={"/managers": "sid"}, fail=True)
    c = _client_with(sio)
    h = SocketIOLogHandler(c)

    async def scenario():
        h.emit(_record("uplink disconnected", level=logging.WARNING))
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert len(h._buffer) == 1, "запис зник: ні відправлений, ні збережений"
    assert h._buffer[0]["msg"] == "uplink disconnected"


def test_buffer_drains_in_the_state_the_library_actually_gives_us():
    """`flush_buffer` мусить працювати в момент, коли його кличуть.

    Порядок у python-socketio: спершу наповнюється `namespaces` і кличеться
    наш обробник `connect` (а з нього — `flush_buffer`), і лише потім
    ставиться `connected = True` у клієнта. Тобто в цю мить прапорець
    клієнта ще `False`.

    Доти готовність визначалась саме по ньому — і буфер не зливався НІКОЛИ,
    на кожному реконекті. Записи лежали до межі 2000 і найстаріші тихо
    випадали (знайдено ревʼю, перевірено на живому сервері).
    """
    import asyncio

    sio = _NamespaceDropClient(namespaces={"/managers": "sid"}, fail=False)
    sio.connected = False  # саме той стан, що буває всередині on("connect")
    c = _client_with(sio)
    h = SocketIOLogHandler(c)
    h._buffer.append(_record("while offline"))

    assert c.connected is True, "готовність визначається неймспейсом, не прапорцем"

    async def scenario():
        # Саме в циклі: поза ним відправка тихо відкидається, і тест
        # проходив би від того, що буфер спорожнів, а не від доставки
        # (знайдено другим колом ревʼю).
        h.flush_buffer()
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert not h._buffer, "буфер не злився в тому стані, у якому його й зливають"
    assert sio.sent and sio.sent[0][1]["msg"] == "while offline", (
        "буфер спорожнів, але запис нікуди не пішов"
    )


def test_stop_really_cancels_emits_still_in_flight():
    """Перевіряємо СТАН задачі, а не те, що набір спорожнів.

    Перша версія перевіряла `not c._tasks` — а `stop()` чистить набір
    беззастережно, тож тест проходив і з прибраним `cancel()` (знайдено
    ревʼю).
    """
    import asyncio

    class _SlowClient(_NamespaceDropClient):
        async def emit(self, event, data, namespace=None):
            await asyncio.sleep(5)  # висить, поки не скасують

    sio = _SlowClient(namespaces={"/managers": "sid"}, fail=False)
    c = _client_with(sio)

    async def scenario():
        c.emit("log", {"msg": "x"})
        task = next(iter(c._tasks))
        await asyncio.sleep(0)
        await c.stop()
        assert task.cancelled(), "задачу не скасовано — лишилась висіти"
        assert not c._tasks

    asyncio.run(scenario())


def test_stop_does_not_orphan_the_log_it_writes_itself():
    """`stop()` не має губити рядок, який сам і породжує.

    Знайдено другим колом ревʼю: `disconnect()` кличе наш обробник
    `on("disconnect")`, той пише «uplink disconnected», і поки handler ще на
    root-логері, цей рядок породжує НОВУ задачу відправки — після того, як
    ми вже зібрали попередні. Вона лишалась без нагляду, а її запис падав у
    буфер уже відчепленого handler-а. Тобто зникав саме той рядок, заради
    якого requeue і робився.
    """
    import asyncio

    class _DisconnectLogsClient(_NamespaceDropClient):
        def __init__(self, client_holder, **kw):
            super().__init__(**kw)
            self._holder = client_holder

        async def disconnect(self):
            # Так робить бібліотека: обробник disconnect кличеться, поки
            # неймспейс ще числиться живим.
            await self._holder[0]._on_disconnect()
            self.connected = False
            self.namespaces = {}

    holder = []
    sio = _DisconnectLogsClient(holder, namespaces={"/managers": "sid"}, fail=True)
    c = _client_with(sio)
    holder.append(c)
    h = c.attach_handler_to_root()
    root = logging.getLogger()

    async def scenario():
        await c.stop()
        # Після stop() не лишається ні задач у дорозі, ні handler-а на логері.
        assert not c._tasks, "лишилась відправка без нагляду"
        assert h not in root.handlers, "handler не відчеплено"

    try:
        asyncio.run(scenario())
    finally:
        if h in root.handlers:
            root.removeHandler(h)


# ---------------------------------------------------------------------------
# PET-928 — stopping must also stop the library's own retry loop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stopping_aborts_a_reconnect_that_is_already_in_flight():
    """A socket mid-reconnect used to come back AFTER we said it was off.

    `AsyncClient.disconnect()` closes a live connection, but it does not touch
    `_reconnect_task`. On shop wi-fi the connection drops all the time, so the
    ordinary case is that the day runs out — or support switches diagnostics
    off — while the library is between retries. The retry then slept out its
    backoff and connected again, with our handlers still bound, while the
    config said «off» and nothing held a handle on that client any more.

    This drives the REAL socketio client, because the bug lives in the
    difference between two of its methods; a fake `_sio` cannot show it.
    """
    import socketio

    from src.services.log_uplink import LogUplinkClient

    client = LogUplinkClient({"url": "https://x", "tenant": "t", "reconnect_delay": 1})
    sio = client._sio
    sio.reconnection_delay = 0.05
    sio.reconnection_delay_max = 0.05
    sio.randomization_factor = 0

    attempts = []

    async def never_connects(*a, **kw):
        attempts.append(1)
        raise socketio.exceptions.ConnectionError("unreachable")

    sio.connect = never_connects

    # The state the library reaches on its own when the network drops.
    sio.eio.state = "connected"
    await sio._handle_eio_disconnect("transport error")
    assert sio._reconnect_task is not None, (
        "precondition failed: no retry loop was started, so this test would "
        "pass without proving anything"
    )

    await asyncio.wait_for(client.stop(), timeout=5)

    settled = len(attempts)
    await asyncio.sleep(0.3)  # several backoff windows at 0.05s
    assert len(attempts) == settled, "the socket came back after stop()"
    assert sio._reconnect_task is None or sio._reconnect_task.done()


@pytest.mark.asyncio
async def test_stopping_drops_the_diagnostics_channel():
    """Commands must not be answerable on a connection we have given up on."""
    from src.services.log_uplink import LogUplinkClient

    client = LogUplinkClient({"url": "https://x", "tenant": "t", "reconnect_delay": 1})

    async def cb(cmd_id, cmd, args):
        return {"cmd_id": cmd_id, "ok": True}

    client.set_diagnostics_callback(cb)
    await asyncio.wait_for(client.stop(), timeout=5)

    emitted = []

    async def fake_emit(event, payload):
        emitted.append(payload)

    client._safe_emit = fake_emit
    await client._on_diagnostic({"cmd_id": "c1", "cmd": "shell", "args": {}})
    assert emitted and emitted[0]["ok"] is False


@pytest.mark.asyncio
async def test_a_drop_arriving_after_stop_does_not_start_a_new_retry_loop():
    """The library's read loop can report the drop after we have torn down.

    Aborting the retry loop that exists is not enough if a late disconnect
    event is still free to start a fresh one — the door would reopen by a
    different route than the one just closed.
    """
    from src.services.log_uplink import LogUplinkClient

    client = LogUplinkClient({"url": "https://x", "tenant": "t", "reconnect_delay": 1})
    sio = client._sio
    await asyncio.wait_for(client.stop(), timeout=5)

    sio.eio.state = "connected"  # what a live socket looks like when it drops
    await sio._handle_eio_disconnect("transport error")

    assert sio._reconnect_task is None, "a late drop started the retry loop again"
