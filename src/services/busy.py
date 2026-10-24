"""Чи менеджер зараз посеред незворотної роботи — і кого через це не чіпати.

BH-164. `POST /system/update` доти не питав НІЧОГО: він запускав інсталятор, а
інсталятор у `PrepareToInstall` (`installers/barhandler-setup.iss`) робить
`taskkill /F /IM bhm.exe`. Тобто кнопка «Оновити» могла вбити процес у будь-який
момент.

Найдорожчий момент — оплата карткою. SSI-обмін іде в кілька кроків
(`src/services/terminals/ssi.py`, `charge`): `Purchase step 1`, опитування
`GetStatus`, далі `step 2` і `GetLastResult`. Якщо процес зникає посередині,
картку може бути СПИСАНО, а каса про результат не дізнається ніколи —
`AcquirerResult` нікуди не повернеться. Втрачений друк — той самий механізм,
лише наслідок дешевший.

І це не «адмін свідомо пішов у службовий дашборд»: кнопку оновлення кличуть
просто з продуктів — `bar-handler-app` (`BARHANDLER_MANAGER_SYSTEM_UPDATE`) і
`petshandler-app` (`manager.post("/system/update")`), тобто вона доступна
посеред зміни.

Через це рішення тут табличне, а не «додати перевірку в /charge»: перелік
критичних маршрутів лежить в одному місці (`CRITICAL_OPERATIONS`), решта
маршрутів приладних роутерів мусить бути названа в `SAFE_OPERATIONS`, і тест
падає на КОЖНОМУ маршруті, якого немає ні там, ні там. Інакше наступний
доданий маршрут тихо лишиться без охорони — а помітити це можна буде тільки
по загубленій транзакції.
"""

from __future__ import annotations

import itertools
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from fastapi import Request

logger = logging.getLogger(__name__)

# Маршрути, під час яких оновлюватись НЕ можна: гроші, фіскальні документи,
# друк. Ключ — (метод, повний шлях, як його бачить FastAPI разом із префіксом
# роутера); значення — текст для людини, який каса покаже у відмові.
CRITICAL_OPERATIONS: dict[tuple[str, str], str] = {
    ("POST", "/terminal/charge"): "оплата карткою",
    # PET-882 — повернення на картку. Гроші їдуть так само, як і при
    # оплаті, тож оновлення посеред нього так само неприпустиме.
    ("POST", "/terminal/refund"): "повернення на картку",
    ("POST", "/terminal/{terminal_id}/cancel"): "скасування оплати на терміналі",
    ("POST", "/print/receipt"): "друк чека",
    ("POST", "/print/fiscal"): "друк фіскального чека",
    ("POST", "/print/text"): "друк",
    ("POST", "/print/lines"): "друк",
    ("POST", "/print/label"): "друк етикетки",
    ("POST", "/print/kitchen"): "друк замовлення на кухню",
    ("POST", "/drawer/open"): "відкриття грошової скриньки",
    ("POST", "/fiscal/it/document"): "реєстрація фіскального документа",
    ("POST", "/fiscal/it/z"): "закриття зміни (Z-звіт)",
    ("POST", "/fiscal/it/x"): "X-звіт",
    ("POST", "/fiscal/it/reprint"): "повторний друк фіскального документа",
}

# Решта маршрутів приладних роутерів — читання стану й налаштування. Їх
# охороняти НЕ можна: дашборд опитує їх постійно, і якби вони вважались
# зайнятістю, оновлення не запустилось би ніколи.
SAFE_OPERATIONS: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/terminal/discover"),
    ("GET", "/terminal"),
    ("POST", "/terminal/register"),
    ("POST", "/terminal/register-manual"),
    ("POST", "/terminal/serial-scan"),
    ("POST", "/terminal/register-serial"),
    ("POST", "/terminal/{terminal_id}/ping"),
    ("GET", "/terminal/{terminal_id}/info"),
    ("GET", "/terminal/{terminal_id}/merchants"),
    ("PUT", "/terminal/{terminal_id}/merchants"),
    ("DELETE", "/terminal/{terminal_id}"),
    ("GET", "/terminal/{terminal_id}/last-result"),
    ("GET", "/fiscal/it/status"),
})


@dataclass(frozen=True)
class BusyOperation:
    """Одна незавершена критична операція."""

    label: str


class BusyTracker:
    """Скільки критичних операцій просто зараз не доведено до кінця.

    Лічильник, а не прапорець: паралельні друк і оплата — звичайна річ, і
    завершення першої не робить менеджер вільним.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[int, BusyOperation] = {}
        self._ids = itertools.count()

    @contextmanager
    def hold(self, label: str) -> Iterator[None]:
        op_id = next(self._ids)
        with self._lock:
            self._active[op_id] = BusyOperation(label=label)
        try:
            yield
        finally:
            # `finally`, а не `else`: операція, що впала, теж завершена, і
            # лишити її в переліку означає заблокувати оновлення назавжди.
            with self._lock:
                self._active.pop(op_id, None)

    def active(self) -> list[BusyOperation]:
        with self._lock:
            return list(self._active.values())

    def why_busy(self) -> Optional[str]:
        """Текст для відмови, або None, якщо менеджер вільний.

        Назви не повторюються: два одночасні друки — це «друк», а не
        «друк, друк».
        """
        labels: list[str] = []
        for op in self.active():
            if op.label not in labels:
                labels.append(op.label)
        if not labels:
            return None
        return ", ".join(labels)


def busy_tracker(request: Request) -> BusyTracker:
    """Трекер із app.state, створюючи його на місці для тестів, які підіймають
    застосунок без нашого lifespan."""
    tracker = getattr(request.app.state, "busy", None)
    if tracker is None:
        tracker = BusyTracker()
        request.app.state.busy = tracker
    return tracker


def operation_keys(method: str, prefix: str, route_path: str) -> tuple[
    tuple[str, str], ...
]:
    """The keys this request could be known by in the tables.

    There are two shapes, and which one a route carries is a detail of the
    framework version. The route objects in `app.routes` hold the FULL path
    («/terminal/charge»), while the routes of the router module itself hold the
    relative one («/charge»), and `scope["route"]` is the former. An earlier
    version of this file added the prefix to whatever it was given; after the
    FastAPI upgrade that produced «/terminal/terminal/charge», no key matched,
    and the guard was off on EVERY critical route — payment, refund, every
    print, the drawer, the fiscal documents (PET-1161).

    So both shapes are tried, and a route that fits neither table is reported
    rather than passed over in silence — that silence is what let the guard sit
    switched off.
    """
    return ((method, route_path), (method, prefix + route_path))


def critical_label(method: str, prefix: str, route_path: str) -> Optional[str]:
    """The text for a person, or None when this route is not a critical one."""
    for key in operation_keys(method, prefix, route_path):
        label = CRITICAL_OPERATIONS.get(key)
        if label is not None:
            return label
    return None


# Routes already reported as unclassified. The dashboard polls some of these
# routes every second or two and the log is uplinked to a server, so a line per
# request would drown the thing it is meant to make visible. Once per route is
# enough: the point is to be told the tables have drifted, not how often.
_reported: set[tuple[str, str]] = set()


def _worth_reporting(method: str, prefix: str, route_path: str) -> bool:
    """Whether to say out loud that this route is in neither table."""
    if route_path:
        keys = operation_keys(method, prefix, route_path)
        if any(key in SAFE_OPERATIONS for key in keys):
            return False
    # No path at all means no route was chosen — the prefix must not be allowed
    # to stand in for one, or «GET ''» under /terminal would read as the
    # classified «GET /terminal» and say nothing.
    once = (method, route_path)
    if once in _reported:
        return False
    _reported.add(once)
    return True


def guard_critical(prefix: str):
    """A router dependency: holds the operation «in progress» for the request.

    It hangs on the ROUTER as a whole rather than on each route, and decides by
    the table — dependencies run AFTER the route has been chosen, so
    `request.scope["route"]` is already known here.

    Which key to look the route up by: see `operation_keys`.
    """

    async def dependency(request: Request):
        route_path = getattr(request.scope.get("route"), "path", "")
        label = critical_label(request.method, prefix, route_path)
        if label is None:
            if _worth_reporting(request.method, prefix, route_path):
                # Neither table knows this route. Either it is new and nobody
                # classified it, or the shape of the path changed again — and
                # both mean the guard is off somewhere it may be needed.
                logger.warning(
                    "critical guard: route %s %s (prefix %s) is in neither "
                    "CRITICAL_OPERATIONS nor SAFE_OPERATIONS — the update is "
                    "not held back for it",
                    request.method,
                    route_path or "<unknown>",
                    prefix,
                )
            yield
            return
        with busy_tracker(request).hold(label):
            yield

    return dependency


def printers_with_pending_jobs(request: Request) -> list[str]:
    """Принтери, у яких лишились недодруковані джоби.

    Окрема від трекера перевірка й НАВМИСНО: трекер знає лише про запити, що
    зараз виконуються, а черга — це властивість самого пристрою. Якщо джоби
    колись почнуть надходити не через HTTP, ця перевірка це побачить, а
    трекер — ні.
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return []
    busy: list[str] = []
    # Разом із викинутими з кешу, що ще дописують (BH-168): перереєстрація
    # принтера посеред друку інакше робила його невидимим для цієї перевірки.
    devices = (
        registry.devices_for_busy()
        if callable(getattr(registry, "devices_for_busy", None))
        else list(getattr(registry, "_devices", {}).values())
    )
    for device in devices:
        pending = getattr(device, "pending_jobs", None)
        if callable(pending) and pending() > 0:
            busy.append(device.name)
    return busy


def busy_refusal(request: Request) -> Optional[dict]:
    """Чому оновлюватись просто зараз не можна — або None.

    BH-164. Оновлення вбиває процес: вінда-інсталятор робить `taskkill /F`,
    мак-пакет і `install.sh` — `pkill`. Доти `/system/update` не питав НІЧОГО,
    і міг зробити це посеред оплати карткою, коли картку вже списано, а
    `AcquirerResult` іще не повернуто.

    Дві незалежні перевірки, бо вони бачать різне: трекер знає про ЗАПИТИ, що
    виконуються (оплата, друк, фіскальний документ), а черга друку — про
    джоби, які ще лежать у самому пристрої.

    Одна функція на двох читачів: `POST /system/update` (відмовляє 409 на
    натискання кнопки) і `GET /busy`, яке питають САМІ ІНСТАЛЯТОРИ перед тим,
    як убивати. Друге без першого не досить — і навпаки: між кнопкою й
    установкою людина проходить майстер, і це вікно нічим не обмежене.
    """
    reasons: list[str] = []
    in_flight = busy_tracker(request).why_busy()
    if in_flight:
        reasons.append(in_flight)
    pending = printers_with_pending_jobs(request)
    if pending:
        reasons.append("друк у черзі: " + ", ".join(sorted(pending)))
    # PET-972 — sales made offline still waiting for the server: an update
    # replaces the offline service and could lose what it has not sent.
    offline = getattr(request.app.state, "offline_service", None)
    queued = (offline.state.extra or {}).get("queued") if offline is not None else None
    if not isinstance(queued, int):
        # The service is down (no secrets) or silent: its queue is on disk,
        # read the same way switching off reads it. Unreadable stays «free»:
        # an update must stay possible exactly when something is broken.
        from src.services import offline_state

        appid = offline_state.active_appid()
        # Called on the event loop: a short wait, never the manager frozen
        # behind a locked file (the service writes in WAL mode, readers do not wait).
        queued = offline_state.queued_on_disk(appid, timeout=0.2, quiet=True) if appid else None
    if isinstance(queued, int) and queued > 0:
        reasons.append(f"офлайн-операції ще не надійшли на сервер: {queued}")
    if not reasons:
        return None
    return {
        "code": "manager_busy",
        # Текст іде прямо в інтерфейс каси й у вікно інсталятора, тож він
        # українською й закінчується тим, що людині робити.
        "message": (
            "Зараз іде " + "; ".join(reasons)
            + ". Оновлення перерве це — спробуйте за хвилину."
        ),
        "busy": reasons,
    }
