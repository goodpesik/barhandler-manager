"""Async Socket.IO client + logging.Handler that streams logs to the
central barhandler-manager-logs server.

Single source of truth for the uplink:
- `SocketIOLogHandler` is a `logging.Handler` — drop it into the root
  logger and any `logger.info(...)` flows through it.
- `LogUplinkClient` owns the `socketio.AsyncClient`, the connect loop,
  the handshake, the `emit_event(...)` helper for business events, and
  the diagnostic-command dispatcher.
"""

from __future__ import annotations

import asyncio
import logging
import platform as _platform
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol


MAX_LINE_BYTES = 4096
BUFFER_LIMIT = 2000


class _Emitter(Protocol):
    connected: bool
    def emit(self, event: str, data: Any) -> Any: ...


class SocketIOLogHandler(logging.Handler):
    """logging.Handler that ships records over a socket.io client.

    Records emitted while the client is disconnected go into a bounded
    deque (oldest dropped on overflow). On reconnect, `flush_buffer()`
    drains the queue in order.
    """

    def __init__(self, client: _Emitter, buffer_limit: int = BUFFER_LIMIT) -> None:
        super().__init__(level=logging.INFO)
        self._client = client
        self._buffer: deque[logging.LogRecord] = deque(maxlen=buffer_limit)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            payload = self._payload(record)
            if getattr(self._client, "connected", False):
                self._client.emit("log", payload)
            else:
                self._buffer.append(record)
        except RecursionError:
            return
        except Exception:
            # Never let logging itself crash the app.
            return

    def flush_buffer(self) -> None:
        while self._buffer and getattr(self._client, "connected", False):
            rec = self._buffer.popleft()
            try:
                self._client.emit("log", self._payload(rec))
            except Exception:
                self._buffer.appendleft(rec)
                return

    def _payload(self, record: logging.LogRecord) -> dict:
        msg = record.getMessage()
        encoded = msg.encode("utf-8", errors="replace")
        if len(encoded) > MAX_LINE_BYTES:
            msg = encoded[:MAX_LINE_BYTES].decode("utf-8", errors="replace") + "... [truncated]"
        return {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        }


class LogUplinkClient:
    """Owns the socket.io AsyncClient, the handshake handler, and the
    business-event / diagnostics dispatch.

    Lifecycle bound to FastAPI lifespan:
        client = LogUplinkClient(uplink_cfg)
        client.attach_handler_to_root()
        await client.start(install_id, version)
        ...
        await client.stop()
    """

    def __init__(self, cfg: dict, log: Optional[logging.Logger] = None) -> None:
        import socketio  # local import — keeps zero cost when uplink disabled
        self._cfg = cfg
        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_delay=max(1, int(cfg.get("reconnect_delay", 2))),
            reconnection_delay_max=60,
            logger=False,
            engineio_logger=False,
        )
        self._handler: Optional[SocketIOLogHandler] = None
        self._log = log or logging.getLogger("uplink")
        self._diagnostics_cb: Optional[Callable[[str, str, dict], Awaitable[dict]]] = None
        self._install_id: str = ""
        self._version: str = ""
        self._tasks: set[asyncio.Task] = set()
        self._sio.on("connect", self._on_connect, namespace="/managers")
        self._sio.on("disconnect", self._on_disconnect, namespace="/managers")
        self._sio.on("diagnostic", self._on_diagnostic, namespace="/managers")

    @property
    def connected(self) -> bool:
        """Чи можна зараз відправляти.

        Дивимось не лише на `connected` клієнта, а й на те, чи піднятий
        НЕЙМСПЕЙС `/managers`. Це різні стани: транспорт уже є, а на
        неймспейс сервер ще не відповів — і `emit` у цей момент кидає
        `BadNamespaceError`. Саме так у клієнта в лог сипалися трейсбеки
        щоразу, коли зʼєднання перепідключалося (а на Android це кожні
        дві-пʼять хвилин).
        """
        if not self._sio.connected:
            return False
        if not hasattr(self._sio, "namespaces"):
            # Старіші версії python-socketio не тримають цього переліку —
            # тоді покладаємось на прапорець клієнта, як раніше. Саме
            # hasattr, а не «порожній перелік»: порожній означає «жоден
            # неймспейс не піднятий», тобто відправляти НЕ можна.
            return True
        return "/managers" in (self._sio.namespaces or {})

    def _spawn_emit(self, event: str, data: Any) -> None:
        """Відправити, не чекаючи — але й не лишаючи виняток без господаря.

        Доти тут було `loop.create_task(self._sio.emit(...))`. Якщо зʼєднання
        падало між перевіркою вище і виконанням задачі, `emit` кидав
        `BadNamespaceError`, задачу ніхто не чекав, і asyncio виливав у лог
        `Task exception was never retrieved` з повним трейсбеком. А цей лог
        іде через наш же handler — тобто шум ще й намагався себе відправити.
        За 50 хвилин у клієнта так набралося 17 циклів із трейсбеками, які
        маскують справжні помилки.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _send() -> None:
            try:
                await self._sio.emit(event, data, namespace="/managers")
            except Exception:
                # Зʼєднання впало між перевіркою і відправкою. Для логів це
                # не втрата: handler покладе запис у буфер і дошле після
                # реконекту. Для подій — тим гірше, але не ціною трейсбека.
                return

        task = loop.create_task(_send())
        # Тримаємо посилання: без нього збирач смiття може прибрати задачу
        # на півдорозі, і Python вивалить "Task was destroyed but it is
        # pending!".
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def emit(self, event: str, data: Any) -> None:
        """Sync entry point used by SocketIOLogHandler.

        We're inside `logging.Handler.emit()` which is a sync call — but
        python-socketio's AsyncClient.emit is a coroutine. Schedule it on
        the running loop without awaiting; if there's no loop (we're
        being called from a non-async context), silently drop.
        """
        if not self.connected:
            return
        self._spawn_emit(event, data)

    def attach_handler_to_root(self) -> SocketIOLogHandler:
        h = SocketIOLogHandler(self)
        logging.getLogger().addHandler(h)
        # Pin noisy loggers so they don't recurse through our handler.
        for name in ("engineio.client", "socketio.client", "engineio", "socketio"):
            logging.getLogger(name).setLevel(logging.WARNING)
        self._handler = h
        return h

    def detach_handler_from_root(self) -> None:
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None

    def set_diagnostics_callback(
        self, cb: Callable[[str, str, dict], Awaitable[dict]],
    ) -> None:
        """Provided by Step 5 (diagnostics module). cb(cmd_id, cmd, args) ->
        awaitable returning the result dict (including cmd_id)."""
        self._diagnostics_cb = cb

    async def start(self, install_id: str, version: str) -> None:
        self._install_id = install_id
        self._version = version
        url = self._cfg["url"]
        try:
            await self._sio.connect(
                url, namespaces=["/managers"], transports=["websocket"],
            )
        except Exception as e:
            self._log.warning(
                f"uplink initial connect failed: {e!r}; "
                "python-socketio will retry in background",
            )

    async def stop(self) -> None:
        try:
            await self._sio.disconnect()
        except Exception:
            pass
        # Незавершені відправки скасовуємо: інакше на виході вони спробують
        # писати в закритий сокет і додадуть той самий шум у лог.
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    async def _safe_emit(self, event: str, data: Any) -> None:
        """Await-версія відправки, яка не перетворює обрив у трейсбек.

        Ці виклики сидять усередині обробників socket.io: виняток звідси
        друкує стек у лог і нічого не лікує — зʼєднання однаково впало, а
        після реконекту handshake піде заново.
        """
        try:
            await self._sio.emit(event, data, namespace="/managers")
        except Exception as e:
            self._log.debug(f"uplink emit {event} skipped: {e!r}")

    async def _on_connect(self) -> None:
        self._log.info(f"uplink connected to {self._cfg['url']}")
        tenant_id = self._cfg.get("tenant_id", "")
        tenant_name = self._cfg.get("tenant_name", "")
        await self._safe_emit("handshake", {
            # install_id is the stable per-install key the logs server
            # groups everything by. tenant_id (appid) + tenant_name say
            # WHO is logged in here; `tenant` is the legacy subdomain
            # field, kept so older logs-server builds still show a label.
            "install_id": self._install_id,
            "tenant_id": tenant_id,
            "tenant_name": tenant_name,
            "tenant": self._cfg.get("tenant", "") or tenant_name,
            "version": self._version,
            "platform": _platform.platform(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        })
        if self._handler is not None:
            self._handler.flush_buffer()

    async def _on_disconnect(self) -> None:
        self._log.warning("uplink disconnected")

    async def _on_diagnostic(self, data: dict) -> None:
        cmd_id = data.get("cmd_id", "")
        cmd = data.get("cmd", "")
        args = data.get("args", {}) or {}
        if self._diagnostics_cb is None:
            await self._safe_emit("diagnostic_result", {
                "cmd_id": cmd_id, "ok": False, "error": "no diagnostics registered",
            })
            return
        try:
            result = await self._diagnostics_cb(cmd_id, cmd, args)
        except Exception as e:
            result = {"cmd_id": cmd_id, "ok": False, "error": f"{type(e).__name__}: {e}"}
        await self._safe_emit("diagnostic_result", result)

    def emit_event(self, event_type: str, **payload: Any) -> None:
        """Fire-and-forget business event. Safe from any async context;
        silently drops if uplink is disabled or offline."""
        if not self.connected:
            return
        self._spawn_emit("event", {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            **payload,
        })


def get_or_create_install_id(install_id_path: Path) -> str:
    if install_id_path.exists():
        return install_id_path.read_text(encoding="utf-8").strip()
    new_id = str(uuid.uuid4())
    install_id_path.write_text(new_id, encoding="utf-8")
    return new_id


# Module-level singleton — set by lifespan, consumed by `emit_event(...)`
# helpers scattered across the codebase (ssi.py, scan.py, etc.).
_active: Optional[LogUplinkClient] = None


def set_active(client: Optional[LogUplinkClient]) -> None:
    global _active
    _active = client


def emit_event(event_type: str, **payload: Any) -> None:
    """Module-level convenience for business events. No-op if uplink
    isn't active. Safe to call from anywhere in the manager."""
    if _active is not None:
        _active.emit_event(event_type, **payload)
