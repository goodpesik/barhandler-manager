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
    def emit(self, event: str, data: Any, requeue: Any = None) -> Any: ...


class SocketIOLogHandler(logging.Handler):
    """logging.Handler that ships records over a socket.io client.

    Records emitted while the client is disconnected go into a bounded
    deque (oldest dropped on overflow). On reconnect, `flush_buffer()`
    drains the queue in order.
    """

    def __init__(self, client: _Emitter, buffer_limit: int = BUFFER_LIMIT) -> None:
        super().__init__(level=logging.INFO)
        self._client = client
        # LogRecord — не відправлені взагалі; dict — зірвались на відправці й
        # повернулись через requeue.
        self._buffer: deque[Any] = deque(maxlen=buffer_limit)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            payload = self._payload(record)
            if getattr(self._client, "connected", False):
                # requeue: якщо зʼєднання впаде між цією перевіркою і самою
                # відправкою, запис повернеться в буфер, а не зникне.
                self._client.emit("log", payload, requeue=self._requeue_payload)
            else:
                self._buffer.append(record)
        except RecursionError:
            return
        except Exception:
            # Never let logging itself crash the app.
            return

    def _requeue_payload(self, payload: Any) -> None:
        """Повернути невідправлений запис у чергу.

        У буфері лежать і LogRecord (не відправлені взагалі), і готові
        payload-и (зірвались на відправці). `_payload()` застосовуємо лише
        до перших — див. `flush_buffer`.
        """
        self._buffer.append(payload)

    def flush_buffer(self) -> None:
        while self._buffer and getattr(self._client, "connected", False):
            item = self._buffer.popleft()
            payload = self._payload(item) if isinstance(item, logging.LogRecord) else item
            try:
                self._client.emit("log", payload, requeue=self._requeue_payload)
            except Exception:
                self._buffer.appendleft(item)
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
        """Чи можна відправляти ЗАРАЗ.

        Питаємо саме про неймспейс `/managers`, а не про прапорець
        `connected` клієнта. Порядок у python-socketio такий (перевірено на
        5.16.4, async_client.py): `_handle_connect` наповнює `namespaces` і
        кличе наш `on("connect")`, і лише ПІСЛЯ повернення з `connect()`
        ставиться `connected = True`.

        Через це `flush_buffer()`, який кличеться саме з `_on_connect`,
        бачив `connected = False` НА КОЖНОМУ реконекті — і буфер не
        зливався ніколи. Записи лежали до межі в 2000 і найстаріші тихо
        випадали. Знайдено ревʼю, яке прогнало це проти живого сервера.
        """
        namespaces = getattr(self._sio, "namespaces", None)
        if namespaces is None:
            # Старіші версії python-socketio не тримають цього переліку.
            return bool(self._sio.connected)
        return "/managers" in namespaces

    def _spawn_emit(
        self, event: str, data: Any, requeue: Optional[Callable[[Any], None]] = None,
    ) -> None:
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
                # Зʼєднання впало між перевіркою і відправкою. Трейсбек тут
                # нічого не лікує, але й губити запис не треба: для логів
                # вертаємо його в буфер, звідки він піде після реконекту.
                #
                # Знайдено ревʼю: без цього найцінніший рядок — «uplink
                # disconnected» — зникав завжди. Його пишуть у момент, коли
                # неймспейс ще числиться живим, тож у буфер він не потрапляв,
                # а відправка вже не вдавалась.
                if requeue is not None:
                    try:
                        requeue(data)
                    except Exception:
                        pass

        task = loop.create_task(_send())
        # Тримаємо посилання: без нього збирач смiття може прибрати задачу
        # на півдорозі, і Python вивалить "Task was destroyed but it is
        # pending!".
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def emit(self, event: str, data: Any, requeue: Optional[Callable[[Any], None]] = None) -> None:
        """Sync entry point used by SocketIOLogHandler.

        We're inside `logging.Handler.emit()` which is a sync call — but
        python-socketio's AsyncClient.emit is a coroutine. Schedule it on
        the running loop without awaiting; if there's no loop (we're
        being called from a non-async context), silently drop.
        """
        if not self.connected:
            return
        self._spawn_emit(event, data, requeue=requeue)

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
        # Спершу знімаємо відправки, що в дорозі, і ДОЧІКУЄМОСЬ їх, і лише
        # потім розриваємо зʼєднання. Обидва порядки тут важливі:
        #
        # • `disconnect()` сам має точки await, і поки він працює, наша
        #   задача може паралельно викликати `emit` — python-socketio прямо
        #   пише, що одночасні emit ламають порядок пакетів;
        # • `cancel()` лише ПРОСИТЬ скасування. Без `gather` задача лишається
        #   pending, і якщо після stop() цикл більше нічого не крутить, Python
        #   на виході друкує «Task was destroyed but it is pending!». Доти це
        #   не вилазило тільки тому, що після нас у lifespan ще були await —
        #   тобто трималось на порядку в чужому файлі (знайдено ревʼю).
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

        try:
            await self._sio.disconnect()
        except Exception:
            pass

        # Handler лишався на root-логері до кінця вимкнення й складав у буфер
        # усе, що логували наступні кроки — буфер, який уже ніхто не зливе.
        self.detach_handler_from_root()

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
