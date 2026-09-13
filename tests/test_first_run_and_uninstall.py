"""BH-150 — перший запуск показує дашборд, кнопка знімає менеджер.

Обидві речі мають одну спільну пастку: вони мусять НІЧОГО не робити там, де
їм не місце. Відкрита вкладка при кожному вході в систему й кнопка видалення,
яка нічого не зносить (бо інсталяція скриптова або це взагалі вінда), — гірше,
ніж їхня відсутність.
"""

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.routes import system as system_routes
from src.services import first_run


# ── перший запуск ──────────────────────────────────────────────────────────


def test_first_run_noop_when_not_frozen(tmp_path, monkeypatch):
    """Запуск із джерел — це робота розробника, браузер йому ні до чого."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    opened: list[str] = []
    monkeypatch.setattr(
        first_run.webbrowser, "open",
        lambda url: (opened.append(url), True)[1],
    )

    first_run.open_dashboard_once(tmp_path, 9999)

    assert opened == []
    assert not (tmp_path / first_run._MARKER_NAME).exists()


def test_first_run_noop_on_windows(tmp_path, monkeypatch):
    """На вінді фінальний екран показує інсталятор Inno."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    opened: list[str] = []
    monkeypatch.setattr(
        first_run.webbrowser, "open",
        lambda url: (opened.append(url), True)[1],
    )

    first_run.open_dashboard_once(tmp_path, 9999)

    assert opened == []


def test_first_run_opens_once_then_never(tmp_path, monkeypatch):
    """Відкриваємо рівно раз: позначка лишається й після перезапусків."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    opened: list[str] = []
    monkeypatch.setattr(
        first_run.webbrowser, "open",
        lambda url: (opened.append(url), True)[1],
    )

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(first_run.urllib.request, "urlopen", lambda *a, **k: _Resp())
    # Потік нам тут не потрібен — цікавить сама логіка, а не паралельність.
    monkeypatch.setattr(
        first_run.threading, "Thread",
        lambda target, **kw: type("T", (), {"start": staticmethod(target)})(),
    )

    first_run.open_dashboard_once(tmp_path, 9999)
    assert opened == ["http://localhost:9999"]
    assert (tmp_path / first_run._MARKER_NAME).exists()

    first_run.open_dashboard_once(tmp_path, 9999)  # другий запуск
    assert len(opened) == 1, "вкладка відкрилась удруге — позначку не перевірили"


def test_first_run_survives_dead_server(tmp_path, monkeypatch):
    """Сервер не піднявся — вкладку не відкриваємо: показати людині
    «не вдається зʼєднатись» гірше, ніж не показати нічого."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    opened: list[str] = []
    monkeypatch.setattr(
        first_run.webbrowser, "open",
        lambda url: (opened.append(url), True)[1],
    )
    monkeypatch.setattr(
        first_run.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no server")),
    )
    monkeypatch.setattr(first_run, "_ATTEMPTS", 2)
    monkeypatch.setattr(first_run, "_STEP_SECONDS", 0)
    monkeypatch.setattr(
        first_run.threading, "Thread",
        lambda target, **kw: type("T", (), {"start": staticmethod(target)})(),
    )

    first_run.open_dashboard_once(tmp_path, 9999)

    assert opened == []
    assert not (tmp_path / first_run._MARKER_NAME).exists()


# ── видалення ──────────────────────────────────────────────────────────────


class _Req:
    """Мінімальний Request: маршрут читає з нього лише заголовок Origin."""

    def __init__(self, origin=None):
        self.headers = {"origin": origin} if origin else {}


@pytest.mark.asyncio
async def test_uninstall_refuses_outside_mac_app(monkeypatch):
    """Скриптову інсталяцію й вінду знімають їхні власні інсталятори."""
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", False)

    with pytest.raises(HTTPException) as err:
        await system_routes.trigger_uninstall(_Req())

    assert err.value.status_code == 400


def test_uninstall_script_order_agent_before_app():
    """Агент знімаємо ПЕРЕД застосунком: KeepAlive підняв би його заново,
    якби файл зник раніше за plist — і launchd крутив би мертвий процес."""
    cmd = system_routes._build_uninstall_script(purge_data=False)

    assert cmd.index("launchctl bootout") < cmd.index("rm -rf \"/Applications")
    # Себе вбиваємо останнім, інакше скрипт не доробить решту.
    assert cmd.rindex("pkill") > cmd.index("rm -rf \"/Applications")


def test_uninstall_keeps_data_by_default():
    """Без явної згоди тека з конфігом і принтерами лишається: це робота,
    яку людина робила руками."""
    keep = system_routes._build_uninstall_script(purge_data=False)
    purge = system_routes._build_uninstall_script(purge_data=True)

    assert str(system_routes.APP_DIR) not in keep
    assert f'rm -rf "{system_routes.APP_DIR}"' in purge


def test_uninstall_removes_the_system_wide_agent_too():
    """Агент могли покласти й у /Library/LaunchAgents — так робить пакет, коли
    установка йде без залогіненого користувача. Лишити його означає, що
    менеджер «повернеться» при наступному вході в систему."""
    cmd = system_routes._build_uninstall_script(purge_data=False)

    assert "/Library/LaunchAgents/com.goodpesik.barhandler-manager.plist" in cmd


def test_uninstall_kill_pattern_targets_only_the_app():
    """pkill мусить влучати саме в бандл. Патерн на кшталт «bhm» зняв би й
    скриптову інсталяцію, якщо людина тримає обидві."""
    cmd = system_routes._build_uninstall_script(purge_data=False)

    assert "Device Handler.app/Contents/MacOS/bhm" in cmd
    assert 'pkill -f "bhm"' not in cmd


def test_uninstall_covers_the_old_bundle_name():
    """До BH-150 бандл звався BarhandlerManager.app, і такі інсталяції в полі
    є. Лишити їх означає лишити робочий агент, який підніме менеджер при
    наступному вході — тобто «видалив, а воно повернулось»."""
    cmd = system_routes._build_uninstall_script(purge_data=False)

    assert '/Applications/Device Handler.app' in cmd
    assert '/Applications/BarhandlerManager.app' in cmd
    assert "BarhandlerManager.app/Contents/MacOS/bhm" in cmd


# ── оновлення мак-застосунку ───────────────────────────────────────────────


def test_mac_app_update_downloads_the_pkg_not_the_script(monkeypatch):
    """Головна знахідка ревʼю. Доти ця гілка провалювалась у POSIX-шлях, тобто
    `curl | bash install.sh` — а це СКРИПТОВА інсталяція, інший спосіб
    установки, який ще й воює за той самий порт. Кнопка «Оновити» в
    мак-застосунку не оновлювала його ніколи."""
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", True)
    monkeypatch.setattr(system_routes.platform, "machine", lambda: "arm64")

    argv, cmd = system_routes._build_update_argv()

    assert argv[0] == "bash"
    assert "device-handler-silicon.pkg" in cmd
    assert "install.sh" not in cmd, "оновлення знову тягне скриптову інсталяцію"
    assert cmd.strip().endswith('.pkg"'), "пакет треба відкрити інсталятором"


def test_mac_app_update_picks_the_machine_architecture(monkeypatch):
    """Silicon-пакет на Intel-маку не встановиться, тож архітектуру беремо з
    машини, а не з конфігу чи назви останньої збірки."""
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", True)
    monkeypatch.setattr(system_routes, "_mac_host_arch", lambda: "intel")

    _, cmd = system_routes._build_update_argv()

    assert "device-handler-intel.pkg" in cmd


def _sysctl(monkeypatch, value: str) -> None:
    monkeypatch.setattr(
        system_routes.subprocess, "run",
        lambda *a, **k: type("R", (), {"stdout": value})(),
    )


def test_host_arch_sees_through_rosetta(monkeypatch):
    """Знайдено ревʼю. platform.machine() віддає архітектуру ПРОЦЕСУ: Intel-збірка
    під Rosetta на Silicon-маку репортує x86_64 — і оновлення назавжди тягло б
    Intel-пакет, тобто машина лишалась би на трансляції довіку."""
    monkeypatch.setattr(system_routes.platform, "machine", lambda: "x86_64")
    _sysctl(monkeypatch, "1")   # 1 = процес транслюється, отже хост arm64

    assert system_routes._mac_host_arch() == "silicon"


def test_host_arch_trusts_machine_without_rosetta(monkeypatch):
    monkeypatch.setattr(system_routes.platform, "machine", lambda: "x86_64")
    _sysctl(monkeypatch, "0")

    assert system_routes._mac_host_arch() == "intel"


def test_host_arch_survives_missing_sysctl_key(monkeypatch):
    """Ключ існує лише на маку й лише під трансляцією — його відсутність не
    має валити оновлення."""
    monkeypatch.setattr(system_routes.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        system_routes.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no sysctl")),
    )

    assert system_routes._mac_host_arch() == "silicon"


@pytest.mark.asyncio
async def test_mac_update_message_does_not_promise_a_restart(monkeypatch, tmp_path):
    """Для мака загальний текст «перезапуститься за ~30 секунд» був брехнею:
    відкривається майстер, і без пароля адміністратора не зміниться нічого."""
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", True)
    monkeypatch.setattr(system_routes, "_mac_host_arch", lambda: "silicon")
    monkeypatch.setattr(system_routes, "_INSTALL_DIR", tmp_path)
    monkeypatch.setattr(system_routes, "_UPDATE_LOG", tmp_path / "update.log")
    monkeypatch.setattr(
        system_routes.subprocess, "Popen",
        lambda argv, **kw: type("P", (), {"pid": 1})(),
    )

    res = await system_routes.trigger_update()

    assert "30 секунд" not in res["message"]
    assert "інсталятор" in res["message"].lower()


# ── захист деструктивної ручки ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uninstall_refuses_foreign_origin(monkeypatch):
    """Ключ API статичний і лежить у відкритому репо, а CORS пускає будь-який
    сайт на *.web.app. Друк із чужої сторінки — прикро; незворотне видалення
    менеджера — ні."""
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", True)

    with pytest.raises(HTTPException) as err:
        await system_routes.trigger_uninstall(_Req("https://evil.web.app"))

    assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_uninstall_allows_local_dashboard(monkeypatch, tmp_path):
    """А зі сторінки самого менеджера — можна."""
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", True)
    monkeypatch.setattr(system_routes, "APP_DIR", tmp_path)
    monkeypatch.setattr(system_routes, "_UNINSTALL_LOG", tmp_path / "uninstall.log")
    started: list[list[str]] = []
    monkeypatch.setattr(
        system_routes.subprocess, "Popen",
        lambda argv, **kw: started.append(argv) or type("P", (), {"pid": 1})(),
    )

    res = await system_routes.trigger_uninstall(_Req("http://localhost:9999"))

    assert res["status"] == "uninstalling"
    assert started and started[0][0] == "bash"


@pytest.mark.asyncio
async def test_uninstall_allows_no_origin(monkeypatch, tmp_path):
    """curl без Origin — це діагностика з машини, її не ріжемо."""
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", True)
    monkeypatch.setattr(system_routes, "APP_DIR", tmp_path)
    monkeypatch.setattr(system_routes, "_UNINSTALL_LOG", tmp_path / "uninstall.log")
    monkeypatch.setattr(
        system_routes.subprocess, "Popen",
        lambda argv, **kw: type("P", (), {"pid": 1})(),
    )

    res = await system_routes.trigger_uninstall(_Req())

    assert res["status"] == "uninstalling"


# ── перший запуск: невдале відкриття лишає другу спробу ────────────────────


def test_first_run_keeps_the_chance_when_browser_fails(tmp_path, monkeypatch):
    """Знайдено ревʼю: позначка стояла ДО відкриття, тож збій браузера
    назавжди забирав другу спробу — і поверталась та сама скарга, з якої все
    почалось: «дашборд ніхто не пропонує»."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(first_run.urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(
        first_run.webbrowser, "open",
        lambda url: (_ for _ in ()).throw(OSError("no browser")),
    )
    monkeypatch.setattr(
        first_run.threading, "Thread",
        lambda target, **kw: type("T", (), {"start": staticmethod(target)})(),
    )

    first_run.open_dashboard_once(tmp_path, 9999)

    assert not (tmp_path / first_run._MARKER_NAME).exists()


def test_first_run_marker_skipped_when_open_returns_false(tmp_path, monkeypatch):
    """webbrowser.open віддає False, коли відкривати нічим — це теж не успіх."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(first_run.urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(first_run.webbrowser, "open", lambda url: False)
    monkeypatch.setattr(
        first_run.threading, "Thread",
        lambda target, **kw: type("T", (), {"start": staticmethod(target)})(),
    )

    first_run.open_dashboard_once(tmp_path, 9999)

    assert not (tmp_path / first_run._MARKER_NAME).exists()
