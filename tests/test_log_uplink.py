import logging

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
    sio = _NamespaceDropClient(namespaces={"/managers": "sid"}, fail=False)
    sio.connected = False  # саме той стан, що буває всередині on("connect")
    c = _client_with(sio)
    h = SocketIOLogHandler(c)
    h._buffer.append(_record("while offline"))

    assert c.connected is True, "готовність визначається неймспейсом, не прапорцем"
    h.flush_buffer()
    assert not h._buffer, "буфер не злився в тому стані, у якому його й зливають"


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
