import logging

from src.services.log_uplink import SocketIOLogHandler, MAX_LINE_BYTES


class FakeClient:
    def __init__(self, connected=True):
        self.connected = connected
        self.sent = []

    def emit(self, event, data):
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
    """Задача мусить зʼїсти виняток сама.

    Якщо ні — asyncio друкує «Task exception was never retrieved» із повним
    стеком, і саме цей шум маскував справжні помилки в логах клієнта.
    """
    import asyncio

    sio = _NamespaceDropClient(namespaces={"/managers": "sid"}, fail=True)
    c = _client_with(sio)

    async def scenario():
        c.emit("log", {"msg": "hello"})
        assert c._tasks, "задачу не зареєстровано — збирач смiття може її прибрати"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Задача завершилась і НЕ лишила по собі винятку.
        done = [t for t in list(c._tasks) + list(getattr(c, "_finished", []))]
        assert not c._tasks, "задача не прибрана з набору після завершення"
        return done

    asyncio.run(scenario())


def test_stop_cancels_emits_still_in_flight():
    import asyncio

    sio = _NamespaceDropClient(namespaces={"/managers": "sid"}, fail=False)
    c = _client_with(sio)

    async def scenario():
        c.emit("log", {"msg": "x"})
        assert c._tasks
        await c.stop()
        assert not c._tasks, "після stop() не лишаємо задач у дорозі"

    asyncio.run(scenario())
