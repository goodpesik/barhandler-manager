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
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from fastapi import Request

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


def guard_critical(prefix: str):
    """Залежність роутера: тримає операцію «в роботі» на час запиту.

    Вішається на РОУТЕР цілком, а не на кожен маршрут, і сама вирішує за
    таблицею — залежності виконуються ПІСЛЯ вибору маршруту, тож
    `request.scope["route"]` тут уже відомий.

    Префікс передається ЯВНО, бо `route.path` у знайденому маршруті —
    відносний («/charge», «/{terminal_id}/cancel»), без префікса роутера, а
    `scope["root_path"]` тут порожній. Спершу тут стояло `route.path` без
    префікса: жоден ключ не збігався, охорона не вмикалась НІ на одному
    маршруті — і всі табличні тести при цьому зеленіли. Знайшов це живий
    запит (`test_a_real_charge_marks_the_manager_busy`).
    """

    async def dependency(request: Request):
        route = request.scope.get("route")
        full_path = prefix + getattr(route, "path", "")
        label = CRITICAL_OPERATIONS.get((request.method, full_path))
        if label is None:
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
