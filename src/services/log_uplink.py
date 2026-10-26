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
    drains the queue.

    Порядок у черзі — приблизний: запис, що зірвався на відправці, вертається
    в кінець уже після того, як туди могли лягти новіші. Для читача це не
    проблема — у payload лежить `ts` самого запису, і сортувати треба по
    ньому, а не по порядку надходження.
    """

    def __init__(self, client: _Emitter, buffer_limit: int = BUFFER_LIMIT) -> None:
        super().__init__(level=logging.INFO)
        self._client = client
        # LogRecord — не відправлені взагалі; dict — зірвались на відправці й
        # повернулись через requeue.
        self._buffer: deque[Any] = deque(maxlen=buffer_limit)
        # PET-1157 — how many lines had no loop to go through. Said once on the
        # next flush: a hole of this kind is otherwise found only by somebody
        # going to look for it, which is how this one was found.
        self._deferred = 0

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            payload = self._payload(record)
            if getattr(self._client, "connected", False):
                # requeue: якщо зʼєднання впаде між цією перевіркою і самою
                # відправкою, запис повернеться в буфер, а не зникне.
                handed = self._client.emit(
                    "log", payload, requeue=self._requeue_payload,
                )
                # PET-1157 — `False` means the client had no event loop to hand
                # the line to (we are in a worker thread and the uplink is not
                # started, or it is already stopping). Such a record used to
                # vanish without a word: the uplink was CONNECTED, so the
                # branch below did not buffer it either. Everything logged off
                # the loop's thread was lost that way — the network scan among
                # it, which is what the remote diagnostics exist for.
                if handed is False:
                    self._buffer.append(payload)
                    self._deferred += 1
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
                if self._client.emit(
                    "log", payload, requeue=self._requeue_payload,
                ) is False:
                    # PET-1157 — no loop to hand it to. Putting it back and
                    # stopping is the only honest move: going on would spin
                    # the whole buffer through the same refusal.
                    self._buffer.appendleft(item)
                    return
            except Exception:
                self._buffer.appendleft(item)
                return
        self._say_deferred()

    def _say_deferred(self) -> None:
        """PET-1157 — one line about the records that had to wait for a loop.

        Written from here, that is from the loop's own thread, and built by
        hand rather than through `logging`: we are inside our own handler, and
        a `logger.warning()` here would come straight back into `emit()`.
        """
        if not self._deferred or not getattr(self._client, "connected", False):
            return
        count, self._deferred = self._deferred, 0
        try:
            self._client.emit("log", {
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": "WARNING",
                "logger": "uplink",
                "msg": (
                    f"uplink: {count} log line(s) had no event loop to go "
                    "through and went from the buffer instead"
                ),
            })
        except Exception:
            self._deferred = count

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
        # PET-1157 — the loop the uplink was started on. A log line written
        # from a worker thread has no running loop of its own, and the scan of
        # the network — the one thing the remote diagnostics are for — runs in
        # exactly such a thread (`asyncio.to_thread`, a blocking TCP sweep).
        self._loop: Optional[asyncio.AbstractEventLoop] = None
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
    ) -> bool:
        """Відправити, не чекаючи — але й не лишаючи виняток без господаря.

        Returns True when the send was handed to the loop, False when there
        was no loop to hand it to (PET-1157): then the caller keeps the line
        instead of losing it.

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
            # PET-1157 — we are NOT on the loop's thread. Until now this was
            # the end of the line: `RuntimeError` → `return`, and the record
            # disappeared — not into the buffer, not into a retry, without a
            # word. Everything logged from a worker thread was lost, so the
            # terminal and printer discovery (both `asyncio.to_thread`,
            # because both are blocking sweeps) never showed up centrally at
            # all, although the machine's own bhm.log had every line.
            loop = self._loop
            if loop is None or loop.is_closed():
                return False
            try:
                loop.call_soon_threadsafe(self._send_on_loop, event, data, requeue)
            except RuntimeError:
                # The loop is on its way out (stopped between the check and
                # now). The caller buffers it.
                return False
            return True
        self._send_on_loop(event, data, requeue)
        return True

    def _send_on_loop(
        self, event: str, data: Any, requeue: Optional[Callable[[Any], None]] = None,
    ) -> None:
        """Start the send. Always runs ON the loop's thread — either called
        straight from async code, or handed over by `call_soon_threadsafe`."""
        loop = asyncio.get_running_loop()

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

    def emit(
        self, event: str, data: Any, requeue: Optional[Callable[[Any], None]] = None,
    ) -> bool:
        """Sync entry point used by SocketIOLogHandler.

        We're inside `logging.Handler.emit()` which is a sync call — but
        python-socketio's AsyncClient.emit is a coroutine, so it is handed to
        the loop without awaiting. PET-1157 — this works from ANY thread now,
        and says so: `False` means there was no loop to hand it to, and the
        handler puts the line back in its buffer rather than losing it.
        """
        if not self.connected:
            return False
        return self._spawn_emit(event, data, requeue=requeue)

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
        # PET-1157 — taken here and not in `__init__`: the client is built
        # before the loop runs, and it is THIS loop that the worker threads
        # need to reach.
        self._loop = asyncio.get_running_loop()
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
        # Порядок тут — увесь зміст методу.
        #
        # 1. Знімаємо відправки, що в дорозі, і ДОЧІКУЄМОСЬ їх. `cancel()` лише
        #    просить скасування; без `gather` задача лишається pending, і Python
        #    на виході друкує «Task was destroyed but it is pending!».
        # 2. Відчіплюємо handler ВІД root-логера — до `disconnect()`, не після.
        #    Знайдено другим колом ревʼю: `disconnect()` сам кличе наш обробник
        #    `on("disconnect")`, той пише «uplink disconnected», і поки handler
        #    ще на місці, цей рядок породжує НОВУ задачу відправки — вже після
        #    того, як ми зібрали попередні. Вона лишалась без нагляду, а її
        #    запис падав у буфер уже відчепленого handler-а, тобто зникав
        #    назавжди. Саме той рядок, заради якого requeue і робився.
        # 3. Розриваємо зʼєднання — тепер логи вимкнення нікуди не відправляються.
        # 4. Другий прохід по задачах: якщо щось усе-таки зʼявилось між
        #    кроками, воно не переживе метод.
        await self._drain_tasks()
        self.detach_handler_from_root()
        # PET-1157 — and no thread may start a new send onto a loop that is
        # closing: from here on such a line is refused, not scheduled.
        self._loop = None
        # The diagnostics channel dies with the socket, not after it. Left in
        # place it would answer commands on any connection that came back.
        self._diagnostics_cb = None
        try:
            # `disconnect()` is NOT enough, and the difference is the whole
            # point of PET-928. It closes a LIVE connection, but if the socket
            # is mid-reconnect at this moment — an ordinary event on shop
            # wi-fi — the library's own retry task is untouched: it sleeps out
            # its backoff and connects again, with our handlers still bound.
            # We would have written «off» in the config, cleared the singleton
            # and told the operator it was off, while a working remote session
            # came back up that nothing holds a handle on any more.
            #
            # `reconnection = False` stops a NEW retry loop from starting, and
            # `shutdown()` aborts one already in flight (it calls
            # `disconnect()` itself when the socket is live, so the ordinary
            # case is unchanged).
            self._sio.reconnection = False
        except Exception:
            pass

        # Kill the retry loop BEFORE asking the library to shut down, and in
        # its own try: `shutdown()` reaches for an abort flag that does not
        # exist until the first successful connect, so on a client that has
        # never connected it raises — and a shared `except` would swallow the
        # cancellation below with it. That is exactly what happened when this
        # was written the other way round.
        #
        # Cancelling is also the half that does not depend on winning a race:
        # `shutdown()` only RAISES the abort flag, and the retry loop clears
        # that flag as its first statement, so a flag raised before the loop
        # body starts is simply wiped.
        task = getattr(self._sio, "_reconnect_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                # Expected: we just cancelled it. Narrower than BaseException
                # so a real interrupt still gets through.
                pass

        # Forget the task BEFORE `shutdown()` runs. The library clears this
        # reference only on its own success path, so the one we just cancelled
        # stays here — and `shutdown()` re-awaits whatever it finds. Awaiting
        # an already-CANCELLED task raises `CancelledError`, which is a
        # BaseException and would sail straight out of `stop()`: the day
        # watcher re-raises it by design and would die for good, leaving this
        # machine with nothing to ever close the door again, and lifespan
        # shutdown would skip everything after `await uplink.stop()`.
        try:
            self._sio._reconnect_task = None
            from socketio import base_client

            if self._sio in base_client.reconnecting_clients:
                base_client.reconnecting_clients.remove(self._sio)
        except Exception:
            pass

        try:
            await self._sio.shutdown()
        except Exception:
            # Not `CancelledError` too: clearing the reference above removes the
            # only way this raised one, and catching it here would swallow a
            # cancellation aimed at whoever called us — lifespan shutdown does
            # exactly that.
            pass

        # Last: a `connect()` interrupted AFTER the transport came up but
        # before the socket.io handshake finished leaves engine.io connected
        # while `connected` stays False, so `shutdown()` takes neither of its
        # branches and that transport lives on — free to finish its handshake
        # later and announce this install to the server again, which is the
        # very thing being switched off here.
        try:
            eio = getattr(self._sio, "eio", None)
            if eio is not None and getattr(eio, "state", "disconnected") != "disconnected":
                await eio.disconnect(abort=True)
        except Exception:
            pass

        await self._drain_tasks()

    async def _drain_tasks(self) -> None:
        """Скасувати відправки в дорозі й дочекатись, поки вони справді стануть."""
        tasks = list(self._tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
        # PET-928 — a command may need to act only AFTER its answer has left,
        # because the action removes the very socket that would carry it.
        # Sleeping «long enough» was a guess; this is the actual send.
        after = result.pop("_after_reply", None) if isinstance(result, dict) else None
        await self._safe_emit("diagnostic_result", result)
        if after is not None:
            # The reply is on the wire; only now may the command do something
            # that takes this socket away.
            try:
                await after()
            except Exception as e:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "post-reply step of %s failed: %s", cmd, e,
                )

    def emit_event(self, event_type: str, **payload: Any) -> None:
        """Fire-and-forget business event. Safe from any thread and from any
        async context; drops the event when the uplink is disabled or offline.

        PET-1157 — an event has no buffer of its own (a log line does), so the
        one case that can still lose it leaves a line instead of nothing: found
        by the review, which pointed out that the commit claimed events took
        the same path as log lines, and only their SCHEDULING did.
        """
        if not self.connected:
            return
        handed = self._spawn_emit("event", {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            **payload,
        })
        if handed is False:
            self._log.warning(
                f"uplink: the event {event_type!r} had no event loop to go "
                "through and was dropped",
            )


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


def get_active() -> Optional[LogUplinkClient]:
    """The client business events are reported through.

    Readable so a shutdown can check whether the singleton still points at the
    client IT stopped: clearing it blindly would silence `emit_event` for a
    session somebody else had just opened.
    """
    return _active


def emit_event(event_type: str, **payload: Any) -> None:
    """Module-level convenience for business events. No-op if uplink
    isn't active. Safe to call from anywhere in the manager."""
    if _active is not None:
        _active.emit_event(event_type, **payload)
