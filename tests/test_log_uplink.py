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
