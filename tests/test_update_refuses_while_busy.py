"""BH-164 — оновлення не має права вбити менеджер посеред незворотної роботи.

`POST /system/update` доти не питав нічого: інсталятор робить
`taskkill /F /IM bhm.exe`, і зробити це могли посеред SSI-обміну з терміналом.
Між `Purchase step 1` і `GetLastResult` картку вже списано, а `AcquirerResult`
іще не повернуто — каса про результат не дізнається ніколи.

Кнопка «Оновити» живе в налаштуваннях каси (`bar-handler-app`,
`petshandler-app`), а не в службовому дашборді, тож натиснути її можуть саме
посеред зміни.
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException

from src.routes import system as system_routes
from src.services.busy import (
    CRITICAL_OPERATIONS,
    SAFE_OPERATIONS,
    BusyTracker,
    busy_tracker,
    guard_critical,
    printers_with_pending_jobs,
)


def _request(**state) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


# --- сама відмова ----------------------------------------------------------


def test_an_idle_manager_updates(monkeypatch, tmp_path):
    spawned: list = []

    class _FakePopen:
        def __init__(self, *a, **kw):
            spawned.append(a)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def poll(self):
            # BH-174 — маршрут питає код повернення: мовчазна смерть дитини
            # більше не виглядає як успішний старт. Жива дитина — `None`.
            return None

    monkeypatch.setattr(system_routes.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(system_routes, "_UPDATE_LOG", tmp_path / "update.log")
    monkeypatch.setattr(system_routes, "IS_WIN", False)
    monkeypatch.setattr(system_routes, "FROZEN", False)
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", False)

    body = asyncio.run(system_routes.trigger_update(_request()))

    assert body["status"] == "updating"
    assert spawned, "вільний менеджер мусить оновлюватись"


def test_a_card_payment_in_flight_refuses_the_update(monkeypatch, tmp_path):
    """Суть тікета. Це саме той випадок, у якому гроші губляться."""
    spawned: list = []
    monkeypatch.setattr(
        system_routes.subprocess, "Popen",
        lambda *a, **kw: spawned.append(a),
    )
    monkeypatch.setattr(system_routes, "_UPDATE_LOG", tmp_path / "update.log")

    tracker = BusyTracker()
    request = _request(busy=tracker)
    with tracker.hold("оплата карткою"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(system_routes.trigger_update(request))

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "manager_busy"
    assert "оплата карткою" in exc.value.detail["message"]
    assert not spawned, "інсталятор запустили попри незавершену оплату"


def test_a_print_job_left_in_the_queue_refuses_the_update(monkeypatch, tmp_path):
    """Друк дешевший за оплату, але губиться так само — разом із процесом."""
    monkeypatch.setattr(
        system_routes.subprocess, "Popen", lambda *a, **kw: None,
    )
    monkeypatch.setattr(system_routes, "_UPDATE_LOG", tmp_path / "update.log")

    device = SimpleNamespace(name="kitchen", pending_jobs=lambda: 2)
    registry = SimpleNamespace(_devices={"kitchen": device})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(system_routes.trigger_update(_request(registry=registry)))

    assert exc.value.status_code == 409
    assert "kitchen" in exc.value.detail["message"]


def test_the_operation_is_released_even_when_it_fails():
    """Операція, що впала, теж завершена. Якби ми знімали її лише на успіху,
    одна помилка оплати заблокувала б оновлення назавжди."""
    tracker = BusyTracker()
    with pytest.raises(RuntimeError):
        with tracker.hold("оплата карткою"):
            raise RuntimeError("термінал відвалився")
    assert tracker.why_busy() is None


def test_parallel_operations_are_counted_not_flagged():
    """Прапорець замість лічильника означав би, що завершення друку робить
    менеджер «вільним» посеред оплати."""
    tracker = BusyTracker()
    with tracker.hold("оплата карткою"):
        with tracker.hold("друк чека"):
            pass
        assert tracker.why_busy() == "оплата карткою"
    assert tracker.why_busy() is None


def test_the_same_label_is_not_repeated():
    """Два одночасні друки — це «друк», а не «друк, друк»: текст іде людині."""
    tracker = BusyTracker()
    with tracker.hold("друк"), tracker.hold("друк"):
        assert tracker.why_busy() == "друк"


def test_pending_jobs_of_a_device_without_the_method_are_ignored():
    """Заглушки принтерів у старих тестах методу не мають — це не причина
    падати на кожному оновленні."""
    device = SimpleNamespace(name="старий")
    request = _request(registry=SimpleNamespace(_devices={"старий": device}))
    assert printers_with_pending_jobs(request) == []


# --- охорона маршрутів -----------------------------------------------------
#
# Тут навмисно НЕ перебираються `app.routes`: у цій версії FastAPI підключені
# роутери лежать там лінивими обгортками без `path`, і перебір тихо давав
# порожній перелік — тобто «усе класифіковано» на будь-якій таблиці. Тому
# маршрути беруться з самих роутерів, а те, що охорону справді повісили,
# перевіряється і по коду server.py, і живим запитом через TestClient.

GUARDED_ROUTERS: list[tuple[str, str]] = [
    ("/print", "print_routes"),
    ("/terminal", "terminal"),
    ("/drawer", "drawer"),
    ("/fiscal/it", "fiscal_it"),
]

# Роутери, яким охорона не потрібна: службові й читання стану.
UNGUARDED_ROUTERS: frozenset[str] = frozenset({
    "health", "version", "dashboard", "devices", "system",
})


def _router_routes() -> list[tuple[str, str]]:
    """(метод, повний шлях) усіх маршрутів приладних роутерів."""
    import importlib

    found: list[tuple[str, str]] = []
    for prefix, module_name in GUARDED_ROUTERS:
        module = importlib.import_module(f"src.routes.{module_name}")
        for route in module.router.routes:
            for method in sorted(getattr(route, "methods", None) or []):
                if method in ("HEAD", "OPTIONS"):
                    continue
                found.append((method, prefix + route.path))
    return found


def test_every_device_route_is_classified() -> None:
    """Головний запобіжник проти «фікса на половину».

    Маршрут, доданий пізніше й не названий ні критичним, ні безпечним, валить
    цей тест. Інакше він тихо лишиться без охорони, і побачити це можна буде
    тільки по загубленій транзакції.
    """
    routes = _router_routes()
    assert routes, "перебір маршрутів нічого не дав — тест нічого не перевіряє"
    unclassified = [
        key for key in routes
        if key not in CRITICAL_OPERATIONS and key not in SAFE_OPERATIONS
    ]
    assert not unclassified, (
        "маршрути не класифіковані — додай їх у CRITICAL_OPERATIONS "
        f"або в SAFE_OPERATIONS: {sorted(unclassified)}"
    )


def test_the_critical_table_has_no_stale_entries() -> None:
    """Перейменований маршрут лишає в таблиці рядок, який більше нікого не
    охороняє, — і виглядає це як робоча охорона."""
    live = set(_router_routes())
    stale = [key for key in CRITICAL_OPERATIONS if key not in live]
    assert not stale, f"у таблиці є маршрути, яких більше немає: {sorted(stale)}"


def test_every_router_either_carries_the_guard_or_is_named_as_safe() -> None:
    """Новий приладний роутер без охорони — це та сама вада знову.

    Перевіряємо КОЖЕН `include_router` у server.py: або на ньому
    `guard_critical`, або його назва є в переліку тих, кому охорона не
    потрібна.
    """
    import re

    source = (
        pathlib.Path(__file__).resolve().parent.parent
        / "src" / "server.py"
    ).read_text(encoding="utf-8")
    lines = re.findall(r"app\.include_router\((.+)\)", source)
    assert len(lines) >= len(GUARDED_ROUTERS) + 1, lines
    for line in lines:
        module = line.split(".router", 1)[0].strip()
        if "guard_critical" in line:
            continue
        assert module in UNGUARDED_ROUTERS, (
            f"роутер {module} підключено без guard_critical і він не названий "
            f"безпечним"
        )


def test_the_guard_prefix_matches_the_router_prefix() -> None:
    """Префікс тепер передається в залежність РУКАМИ, тобто та сама правда
    записана двічі в одному рядку. Розбіжність тиха: охорона просто перестане
    вмикатись на цьому роутері, а таблиця лишиться на вигляд правильною."""
    import re

    source = (
        pathlib.Path(__file__).resolve().parent.parent / "src" / "server.py"
    ).read_text(encoding="utf-8")
    pairs = re.findall(
        r'prefix="([^"]+)".*?guard_critical\("([^"]+)"\)', source,
    )
    assert len(pairs) == len(GUARDED_ROUTERS), pairs
    for router_prefix, guard_prefix in pairs:
        assert router_prefix == guard_prefix, (
            f"роутер підключено з префіксом {router_prefix!r}, а охорону "
            f"налаштовано на {guard_prefix!r}"
        )


def test_money_and_paper_routes_are_critical() -> None:
    """Перелік, який мусить бути під охороною, — названий прямо, а не виведений
    із самої таблиці: інакше тест погодиться з будь-яким її станом."""
    for key in [
        ("POST", "/terminal/charge"),
        ("POST", "/terminal/refund"),
        ("POST", "/terminal/{terminal_id}/cancel"),
        ("POST", "/print/receipt"),
        ("POST", "/print/fiscal"),
        ("POST", "/print/kitchen"),
        ("POST", "/drawer/open"),
        ("POST", "/fiscal/it/document"),
        ("POST", "/fiscal/it/z"),
    ]:
        assert key in CRITICAL_OPERATIONS, key


def test_polled_routes_are_not_critical() -> None:
    """Дашборд опитує стан постійно. Якби читання вважалось зайнятістю,
    оновлення не запустилось би НІКОЛИ — і це гірше за вихідну ваду."""
    for key in [
        ("GET", "/terminal/{terminal_id}/info"),
        ("GET", "/terminal/{terminal_id}/last-result"),
        ("GET", "/fiscal/it/status"),
    ]:
        assert key not in CRITICAL_OPERATIONS, key


def test_the_tracker_is_created_on_demand() -> None:
    """Тести підіймають застосунок без нашого lifespan — відсутність трекера не
    має валити оновлення."""
    request = _request()
    assert isinstance(busy_tracker(request), BusyTracker)
    assert busy_tracker(request) is request.app.state.busy


# --- живий запит: чи охорона взагалі вмикається -----------------------------
#
# Найважливіше в цьому файлі. Усі таблиці вище перевіряють БУХГАЛТЕРІЮ: що
# шлях названий, що рядок є. Якби `guard_critical` не спрацьовував ні на
# одному маршруті (наприклад, `request.scope["route"].path` виявився без
# префікса роутера), вони б так само зеленіли. Тому нижче — справжній
# POST /terminal/charge і справжній POST /system/update.


@pytest.fixture
def client_with_terminal(config: dict):
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from src.models.terminal import (
        TerminalDescriptor,
        TerminalKind,
        TerminalNetworkAddress,
        TerminalTransport,
    )
    from src.server import create_app

    terminal = TerminalDescriptor(
        id="abc123def456",
        transport=TerminalTransport.network,
        label="Mono POS @ 10.0.0.42",
        kind=TerminalKind.mono_pos,
        model="Verifone X990",
        serial="V1E0207420",
        network=TerminalNetworkAddress(host="10.0.0.42", port=3000),
    )
    with patch("src.devices.scan.discover_usb", return_value=[]), \
         patch("src.devices.scan.discover_network", return_value=[]), \
         patch("src.devices.scan.discover_bluetooth", return_value=[]), \
         patch(
             "src.devices.scan.discover_network_terminals",
             return_value=[terminal],
         ):
        app = create_app(config)
        with TestClient(app) as c:
            c.post("/terminal/discover", headers={"X-Api-Key": _KEY})
            c.post(
                "/terminal/register",
                headers={"X-Api-Key": _KEY},
                json={
                    "id": terminal.id,
                    "kind": "mono_pos",
                    "default_merchant_id": "000000060007176",
                },
            )
            yield c


from src.constants import DEFAULT_API_KEY as _KEY


def test_a_real_charge_marks_the_manager_busy(client_with_terminal) -> None:
    """Охорона мусить тримати операцію САМЕ ПІД ЧАС запиту — і відпускати
    після. Спостерігаємо зсередини, з місця, де адаптер говорить із
    терміналом."""
    from unittest.mock import patch

    from src.models.terminal import AcquirerResult

    app = client_with_terminal.app
    seen: list = []

    async def _charge(self, payload):
        seen.append(app.state.busy.why_busy())
        return AcquirerResult(status="ok", raw_transaction_result="APPROVED")

    with patch("src.services.terminals.ssi.SSITerminalAdapter.charge", new=_charge):
        response = client_with_terminal.post(
            "/terminal/charge",
            headers={"X-Api-Key": _KEY},
            json={"amount_kopecks": 24500, "transaction_uid": "u-1"},
        )

    assert response.status_code == 200, response.text
    assert seen == ["оплата карткою"], (
        "під час оплати менеджер не вважається зайнятим — охорона не "
        f"спрацювала: {seen}"
    )
    assert app.state.busy.why_busy() is None, "операцію не відпустили"


def test_a_read_only_request_does_not_mark_the_manager_busy(
    client_with_terminal,
) -> None:
    """Дзеркало попереднього. Якби читання теж вважалось зайнятістю, дашборд
    своїм опитуванням заблокував би оновлення назавжди."""
    from unittest.mock import patch

    app = client_with_terminal.app
    seen: list = []

    async def _get_info(self):
        seen.append(app.state.busy.why_busy())
        return {"model": "X990"}

    with patch(
        "src.services.terminals.ssi.SSITerminalAdapter.get_info", new=_get_info,
    ):
        client_with_terminal.get(
            "/terminal/abc123def456/info", headers={"X-Api-Key": _KEY},
        )

    assert seen == [None], f"читання стану вважається зайнятістю: {seen}"


def test_the_update_endpoint_answers_409_over_http(client_with_terminal) -> None:
    """Відмова мусить доїхати до каси саме як 409 із текстом, а не як 500."""
    app = client_with_terminal.app
    with app.state.busy.hold("оплата карткою"):
        response = client_with_terminal.post(
            "/system/update", headers={"X-Api-Key": _KEY},
        )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "manager_busy"
    assert "оплата карткою" in detail["message"]
    assert "спробуйте за хвилину" in detail["message"]


# --- лічильник джобів принтера ----------------------------------------------
#
# `pending_jobs()` — друга, незалежна від трекера перевірка. Якщо
# `_job_in_flight` колись лишиться True, оновлення стане неможливим НАЗАВЖДИ,
# і зовні це виглядатиме як «кнопка не працює».


@pytest.mark.asyncio
async def test_pending_jobs_drops_back_to_zero_after_a_normal_print() -> None:
    from src.devices.printer import PrinterDevice

    from tests.test_tspl_receipt import _FakeEscpos

    dev = PrinterDevice("t", {"enabled": True, "paper_width": 80, "protocol": "escpos"})
    dev._printer = _FakeEscpos()
    assert dev.pending_jobs() == 0

    dev._worker_task = asyncio.create_task(dev._worker())
    try:
        await dev.enqueue(lambda _esc: asyncio.sleep(0))
        assert dev.pending_jobs() == 0, "джоб доробив, а принтер досі «зайнятий»"
    finally:
        dev._worker_task.cancel()


@pytest.mark.asyncio
async def test_pending_jobs_drops_back_to_zero_after_a_failed_print() -> None:
    """Джоб, що впав, теж завершений. Це найімовірніший шлях до вічної
    зайнятості: паперу немає — і оновитись більше не можна."""
    from src.devices.printer import PrinterDevice

    from tests.test_tspl_receipt import _FakeEscpos

    dev = PrinterDevice("t", {"enabled": True, "paper_width": 80, "protocol": "escpos"})
    dev._printer = _FakeEscpos()
    dev._worker_task = asyncio.create_task(dev._worker())

    async def _boom(_esc):
        raise RuntimeError("папір скінчився")

    try:
        with pytest.raises(RuntimeError):
            await dev.enqueue(_boom)
        assert dev.pending_jobs() == 0, "після падіння друку принтер лишився зайнятим"
    finally:
        dev._worker_task.cancel()


@pytest.mark.asyncio
async def test_pending_jobs_drops_back_to_zero_when_the_worker_is_cancelled() -> None:
    """Перереєстрація принтера скасовує воркер посеред джоба — і робить це
    тепер на КОЖНУ зміну налаштувань, не лише на видалення."""
    from src.devices.printer import PrinterDevice, PrinterUnavailable

    from tests.test_tspl_receipt import _FakeEscpos

    dev = PrinterDevice("t", {"enabled": True, "paper_width": 80, "protocol": "escpos"})
    dev._printer = _FakeEscpos()
    started = asyncio.Event()

    async def _slow(_esc):
        started.set()
        await asyncio.sleep(30)

    dev._worker_task = asyncio.create_task(dev._worker())
    waiter = asyncio.create_task(dev.enqueue(_slow))
    await asyncio.wait_for(started.wait(), timeout=2)
    assert dev.pending_jobs() == 1, "друк іде, а принтер не вважається зайнятим"

    await dev.disconnect()
    with pytest.raises(PrinterUnavailable):
        await asyncio.wait_for(waiter, timeout=2)

    assert dev.pending_jobs() == 0, (
        "скасований воркер лишив принтер «зайнятим» — оновитись стало б "
        "неможливо до перезапуску менеджера"
    )


# --- інсталятори питають ПЕРЕД тим, як убивати ------------------------------
#
# Найважливіша знахідка ревʼю. Перевірка в `/system/update` закриває лише мить
# натискання кнопки. А з BH-161 інсталятор на вінді (і давно вже .pkg на маку)
# ВІДКРИВАЄТЬСЯ перед людиною — і реальний `taskkill`/`pkill` відбувається
# тоді, коли вона дійде до кроку установки. Дашборд дає їй на це 20 хвилин.
# За 20 хвилин цілком починається оплата карткою.


_INSTALLERS = pathlib.Path(__file__).resolve().parent.parent / "installers"


def test_the_busy_endpoint_needs_no_api_key(client_with_terminal) -> None:
    """Інсталятори ключа не мають — і не мусять. Відповідь не розкриває
    нічого, крім «зайнятий/вільний», рівно як /health і /version."""
    response = client_with_terminal.get("/busy")
    assert response.status_code == 200, response.text
    assert response.json()["busy"] is False


def test_the_busy_endpoint_reports_a_reason(client_with_terminal) -> None:
    app = client_with_terminal.app
    with app.state.busy.hold("оплата карткою"):
        body = client_with_terminal.get("/busy").json()
    assert body["busy"] is True
    assert "оплата карткою" in body["message"]
    assert body["reasons"] == ["оплата карткою"]


def test_the_endpoint_and_the_refusal_come_from_one_place(client_with_terminal) -> None:
    """Два читачі, одна функція. Якби вони розʼїхались, інсталятор ставив би
    поверх оплати, яку каса щойно назвала причиною відмови."""
    app = client_with_terminal.app
    with app.state.busy.hold("друк чека"):
        endpoint = client_with_terminal.get("/busy").json()["message"]
        refusal = client_with_terminal.post(
            "/system/update", headers={"X-Api-Key": _KEY},
        ).json()["detail"]["message"]
    assert endpoint == refusal


def test_the_windows_installer_asks_before_it_kills() -> None:
    """`PrepareToInstall` мусить спитати ДО `CleanPrevious`, а непорожній
    Result перериває установку, нічого не зачепивши."""
    iss = (_INSTALLERS / "barhandler-setup.iss").read_text(encoding="utf-8")
    assert "/busy" in iss, "інсталятор нікого не питає"
    body = iss.split("function PrepareToInstall", 1)[1]
    ask = body.index("ManagerBusyReason")
    kill = body.index("CleanPrevious")
    assert ask < kill, "спитали вже після того, як убили — сенсу нуль"
    assert "Result := BusyReason" in body, "причина не перериває установку"


def test_the_windows_installer_installs_when_the_manager_cannot_answer() -> None:
    """Не запущений менеджер, стара версія без /busy, зайнятий порт — це
    «вільно». Інакше ми зробили б оновлення неможливим саме там, де воно
    найпотрібніше."""
    iss = (_INSTALLERS / "barhandler-setup.iss").read_text(encoding="utf-8")
    fn = iss.split("function ManagerBusyReason", 1)[1].split("\nfunction ", 1)[0]
    assert "catch { }" in fn, "помилка запиту мусить означати «вільно»"
    assert "if not FileExists(TmpFile) then\n    Exit;" in fn, (
        "немає відповіді — треба виходити з порожнім Result"
    )


def _wait_function(script: str) -> str:
    """Тіло `wait_until_manager_free` — і лише воно.

    Друге коло ревʼю: спершу стеля шукалась регекспом по ВСЬОМУ файлу. Це
    крихкий контракт — будь-яке інше числове порівняння більше за 180 тримало б
    тест зеленим, навіть якби справжня стеля впала нижче за оплату.
    """
    text = (_INSTALLERS / script).read_text(encoding="utf-8")
    assert "wait_until_manager_free() {" in text, f"{script}: немає функції очікування"
    return text.split("wait_until_manager_free() {", 1)[1].split("\n}", 1)[0]


@pytest.mark.parametrize("script", ["mac-postinstall.sh", "install.sh"])
def test_the_posix_installers_wait_for_a_free_manager(script: str) -> None:
    """Перервати установку тут уже не можна — файли покладені. Тому чекаємо, і
    стеля мусить перевищувати максимум опитування терміналу (180 с у SSI)."""
    import re

    text = (_INSTALLERS / script).read_text(encoding="utf-8")
    assert "127.0.0.1:9999/busy" in text, f"{script} нікого не питає"
    assert "\nwait_until_manager_free\n" in text, (
        f"{script}: функція є, а викликати її забули"
    )
    caps = [int(n) for n in re.findall(r"-lt (\d+) \]", _wait_function(script))]
    assert caps, f"{script}: у циклі очікування немає числової стелі"
    assert all(c > 180 for c in caps), (
        f"{script}: стеля очікування {caps} не перевищує 180 с — максимум "
        f"опитування терміналу, тобто чекати ми можемо менше, ніж триває оплата"
    )


@pytest.mark.parametrize("script", ["mac-postinstall.sh", "install.sh"])
def test_the_posix_installers_treat_a_silent_manager_as_free(script: str) -> None:
    """Немає curl, немає менеджера, стара версія без /busy — ставимо далі."""
    text = (_INSTALLERS / script).read_text(encoding="utf-8")
    fn = text.split("manager_busy() {", 1)[1].split("\n}", 1)[0]
    assert "command -v curl" in fn, "без curl треба вважати, що вільно"
    assert "return 1" in fn
    assert "-f" in fn and "--max-time" in fn, (
        "запит без таймауту може повісити інсталятор"
    )


def test_the_ssi_poll_ceiling_is_what_the_installers_wait_out() -> None:
    """Зв'язок, який легко розсинхронити: стеля очікування в інсталяторах
    узята з максимуму опитування терміналу. Якщо той виросте, чекати стане
    замало — і цей тест про це скаже."""
    from src.services.terminals import ssi

    assert ssi.STATUS_POLL_MAX_S <= 240, (
        f"опитування терміналу тепер до {ssi.STATUS_POLL_MAX_S}с, а інсталятори "
        f"чекають 240с — підніми стелю в mac-postinstall.sh та install.sh"
    )


def test_the_safe_table_has_no_stale_entries() -> None:
    """Дзеркало тесту для критичних. Без нього застарілий рядок у
    SAFE_OPERATIONS не спіймав би НІХТО: тест на класифікацію перебирає
    маршрути, а не записи таблиці, — тобто зайвий рядок там живе вічно й
    виглядає як усвідомлене рішення."""
    live = set(_router_routes())
    stale = [key for key in SAFE_OPERATIONS if key not in live]
    assert not stale, f"у переліку безпечних є маршрути, яких немає: {sorted(stale)}"


@pytest.mark.asyncio
async def test_a_dead_worker_does_not_block_updates_forever() -> None:
    """Воркер уміє померти сам, і джоби, покладені після цього, лежали б у
    черзі до перестворення пристрою. Раніше це нічого не міняло; тепер на цей
    лічильник дивиться оновлення — і «зайнятий вічно» гірше за вихідну ваду."""
    from src.devices.printer import PrinterDevice

    from tests.test_tspl_receipt import _FakeEscpos

    dev = PrinterDevice("t", {"enabled": True, "paper_width": 80, "protocol": "escpos"})
    dev._printer = _FakeEscpos()

    # Воркер, який уже завершився, плюс джоб, що лишився в черзі.
    dev._worker_task = asyncio.create_task(asyncio.sleep(0))
    await dev._worker_task
    await dev._queue.put((lambda _esc: None, asyncio.get_running_loop().create_future()))

    assert dev._queue.qsize() == 1, "передумова тесту не склалась"
    assert dev.pending_jobs() == 0, (
        "мертвий воркер тримає менеджер «зайнятим» — оновитись стало б "
        "неможливо до перезапуску"
    )


@pytest.mark.asyncio
async def test_a_socket_error_wakes_the_jobs_still_queued_behind_it() -> None:
    """Воркер виходить на `OSError`, і джоби, що СТОЯТЬ У ЧЕРЗІ за ним, не
    виконає вже ніхто. Доти їхні запити висли до таймауту клієнта — а з BH-164
    такий запит ще й тримає менеджер «зайнятим», тобто оновитись стало б
    неможливо до перезапуску. Те саме вже полікували для скасування воркера;
    цей шлях лишався.

    Порядок тут — не косметика. Перша версія цього тесту створювала другий
    джоб через `create_task` і покладалась на те, що він встигне стати в чергу.
    Він НЕ встигав: `enqueue` бачив уже занулений `_printer` і падав сам, тобто
    тест проходив і з поверненою вадою. Тому нижче джоб кладеться в чергу
    ЯВНО, і лише тоді першому дозволено впасти.
    """
    from src.devices.printer import PrinterDevice, PrinterUnavailable

    from tests.test_tspl_receipt import _FakeEscpos

    dev = PrinterDevice("t", {"enabled": True, "paper_width": 80, "protocol": "escpos"})
    dev._printer = _FakeEscpos()
    first_started = asyncio.Event()
    may_fail = asyncio.Event()

    async def _socket_dies(_esc):
        first_started.set()
        await may_fail.wait()
        raise OSError("зʼєднання зникло")

    dev._worker_task = asyncio.create_task(dev._worker())
    doomed = asyncio.create_task(dev.enqueue(_socket_dies))
    await asyncio.wait_for(first_started.wait(), timeout=2)

    # Джоб, який ЧЕКАЄ у черзі — ставимо напряму, без `enqueue`, щоб не
    # залежати від того, чи встиг він до занулення `_printer`.
    behind: asyncio.Future = asyncio.get_running_loop().create_future()

    async def _never_runs(_esc):
        raise AssertionError("цей джоб не мав виконатись")

    await dev._queue.put((_never_runs, behind))
    assert dev._queue.qsize() == 1, "передумова тесту не склалась"

    may_fail.set()
    with pytest.raises(PrinterUnavailable):
        await asyncio.wait_for(doomed, timeout=2)
    with pytest.raises(PrinterUnavailable):
        await asyncio.wait_for(behind, timeout=2)

    assert dev.pending_jobs() == 0


# --- видалення теж незворотне -----------------------------------------------


def test_uninstall_refuses_while_busy(monkeypatch, tmp_path) -> None:
    """Знайдено другим колом ревʼю. Видалення робить той самий `pkill`, що й
    оновлення, — а перевірки не мало взагалі. Ціна тут навіть вища: після
    видалення менеджер не повернеться сам, тобто незавершену оплату вже нічим
    не добити."""
    spawned: list = []
    monkeypatch.setattr(
        system_routes.subprocess, "Popen", lambda *a, **kw: spawned.append(a),
    )
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", True)
    monkeypatch.setattr(system_routes, "_UNINSTALL_LOG", tmp_path / "uninstall.log")

    tracker = BusyTracker()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(busy=tracker)),
        headers={},
    )
    with tracker.hold("оплата карткою"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(system_routes.trigger_uninstall(request))

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "manager_busy"
    assert not spawned, "видалення пішло попри незавершену оплату"


def test_uninstall_works_when_the_manager_is_idle(monkeypatch, tmp_path) -> None:
    """Дзеркало: перевірка не має ламати саме видалення."""
    spawned: list = []

    class _FakePopen:
        def __init__(self, *a, **kw):
            spawned.append(a)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def poll(self):
            # BH-174 — маршрут питає код повернення: мовчазна смерть дитини
            # більше не виглядає як успішний старт. Жива дитина — `None`.
            return None

    monkeypatch.setattr(system_routes.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", True)
    monkeypatch.setattr(system_routes, "_UNINSTALL_LOG", tmp_path / "uninstall.log")

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()), headers={},
    )
    body = asyncio.run(system_routes.trigger_uninstall(request))
    assert body["status"] == "uninstalling"
    assert spawned


def test_every_irreversible_endpoint_asks_busy_refusal() -> None:
    """Запобіжник проти наступного такого пропуску. Перелічує обробники, що
    запускають зовнішню команду, і вимагає, щоб кожен спитав `busy_refusal` —
    інакше третій такий endpoint додадуть так само тихо."""
    import inspect

    source = inspect.getsource(system_routes)
    for handler in ("async def trigger_update", "async def trigger_uninstall"):
        body = source.split(handler, 1)[1].split("\n@router", 1)[0]
        assert "busy_refusal(request)" in body, (
            f"{handler} запускає незворотну команду, не спитавши про зайнятість"
        )


_SHARED_START = "# ── BH-164:"
_SHARED_END = "# ── кінець спільного блоку BH-164 ──"


def _shared_block(script: str) -> str:
    text = (_INSTALLERS / script).read_text(encoding="utf-8")
    start = text.index(_SHARED_START)
    end = text.index(_SHARED_END) + len(_SHARED_END)
    return text[start:end]


def test_the_two_installer_copies_have_not_drifted() -> None:
    """Той самий блок лежить у двох скриптах, і спільного файлу тут бути не
    може: один завантажують рядком із GitHub, другий лежить усередині .pkg.

    Друге коло ревʼю: копії вже розійшлись одразу після написання — різні імена
    змінної, різна структура, різний режим скрипта. Поводились однаково, але це
    саме той стан, із якого правку роблять в одній копії й забувають про другу.
    Тому порівнюємо посимвольно.
    """
    a = _shared_block("install.sh")
    b = _shared_block("mac-postinstall.sh")
    assert a == b, "спільний блок BH-164 розійшовся між install.sh і mac-postinstall.sh"


def test_the_windows_uninstaller_asks_before_it_kills() -> None:
    """Знайдено другим колом ревʼю: гейт стояв лише на установці, а [UninstallRun]
    робить той самий `taskkill /F`. Видалення посеред оплати гірше за оновлення —
    менеджер після нього не повернеться сам."""
    iss = (_INSTALLERS / "barhandler-setup.iss").read_text(encoding="utf-8")
    assert "function InitializeUninstall" in iss, "видалення нікого не питає"
    fn = iss.split("function InitializeUninstall", 1)[1].split("\nend;", 1)[0]
    assert "ManagerBusyReason" in fn
    assert "Result := False" in fn, "причина не скасовує видалення"
    assert "MsgBox" in fn, "людина не побачить, чому видалення не пішло"


def test_the_windows_installer_writes_the_reason_without_a_bom() -> None:
    """Знайдено другим колом ревʼю: `Set-Content -Encoding UTF8` у powershell 5.1
    додає BOM (а `UTF8NoBOM` там не існує), і він поїхав би першим символом у
    діалог, де людина найбільше потребує чіткого тексту."""
    iss = (_INSTALLERS / "barhandler-setup.iss").read_text(encoding="utf-8")
    fn = iss.split("function ManagerBusyReason", 1)[1].split("\nfunction ", 1)[0]
    # Дивимось на ВИКЛИК, а не на слово: у коментарі поруч `Set-Content`
    # згадується саме як те, чого робити не треба. Прибирати коментарі Pascal
    # регекспом тут не можна — фігурні дужки є й усередині рядка PowerShell.
    assert "Set-Content -LiteralPath" not in fn, (
        "Set-Content -Encoding UTF8 на powershell 5.1 додає BOM"
    )
    assert "[System.IO.File]::WriteAllText(" in fn, "файл пишеться не тим способом"
    assert "UTF8Encoding($false)" in fn, "кодування без BOM не задано"
    assert "65279" in fn, "немає запобіжника, що знімає BOM, якщо він усе-таки приїде"


# --- shell-логіка manager_busy: ПОВЕДІНКА, а не текст -----------------------
#
# Друге коло ревʼю показало, що «менеджер не відповів» і «менеджера немає» — це
# різні речі. Синхронний USB-запис блокує весь цикл подій, тобто менеджер, який
# ЗАРАЗ друкує, може не встигнути відповісти за 3 с. Якби мовчання означало
# «вільно», інсталятор убивав би його саме в найгірший момент.
#
# Перевіряємо це не грепом по скрипту, а запуском самої функції з підробленим
# curl у PATH — інакше ми б стверджували наявність рядка, а не поведінку.


def _extract_shell_function(script: str, name: str) -> str:
    text = (_INSTALLERS / script).read_text(encoding="utf-8")
    head = f"{name}() {{"
    assert head in text, f"{script}: немає функції {name}"
    body = text.split(head, 1)[1].split("\n}", 1)[0]
    return head + body + "\n}\n"


@pytest.mark.parametrize(
    ("curl_script", "expected_busy", "case"),
    [
        ("exit 28", True, "таймаут: порт слухають, але не відповіли — чекаємо"),
        ("exit 7", False, "не підключились: менеджера немає — ставимо"),
        ("exit 22", False, "HTTP-помилка: стара версія без /busy — ставимо"),
        ('printf %s \'{"busy":true}\'', True, "сказав «зайнятий»"),
        ('printf %s \'{"busy":false}\'', False, "сказав «вільний»"),
        ('printf %s \'{"busy": true}\'', True, "пробіл після двокрапки"),
    ],
)
def test_manager_busy_shell_function(
    tmp_path, curl_script: str, expected_busy: bool, case: str,
) -> None:
    import os
    import subprocess

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(f"#!/bin/sh\n{curl_script}\n", encoding="utf-8")
    curl.chmod(0o755)

    fn = _extract_shell_function("install.sh", "manager_busy")
    script = tmp_path / "probe.sh"
    script.write_text(
        "set -eu\n" + fn + "\nif manager_busy; then exit 10; else exit 20; fi\n",
        encoding="utf-8",
    )

    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")
    rc = subprocess.run(["bash", str(script)], env=env, check=False).returncode
    assert rc in (10, 20), f"функція впала (rc={rc})"
    assert (rc == 10) is expected_busy, case


def test_manager_busy_says_free_without_curl(tmp_path) -> None:
    """Termux та голі системи без curl не мусять лишитись без установки."""
    import subprocess

    fn = _extract_shell_function("install.sh", "manager_busy")
    script = tmp_path / "probe.sh"
    script.write_text(
        "set -eu\n" + fn + "\nif manager_busy; then exit 10; else exit 20; fi\n",
        encoding="utf-8",
    )
    # PATH із самою лише текою, де curl немає. Абсолютний шлях до bash — бо
    # інакше не знайдеться вже сам інтерпретатор.
    import shutil

    empty = tmp_path / "empty-bin"
    empty.mkdir()
    bash = shutil.which("bash")
    assert bash, "у системі немає bash"
    rc = subprocess.run(
        [bash, str(script)], env={"PATH": str(empty)}, check=False,
    ).returncode
    assert rc == 20, "без curl треба вважати, що менеджер вільний"
