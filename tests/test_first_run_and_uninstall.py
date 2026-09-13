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
    monkeypatch.setattr(first_run.webbrowser, "open", lambda url: opened.append(url))

    first_run.open_dashboard_once(tmp_path, 9999)

    assert opened == []
    assert not (tmp_path / first_run._MARKER_NAME).exists()


def test_first_run_noop_on_windows(tmp_path, monkeypatch):
    """На вінді фінальний екран показує інсталятор Inno."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    opened: list[str] = []
    monkeypatch.setattr(first_run.webbrowser, "open", lambda url: opened.append(url))

    first_run.open_dashboard_once(tmp_path, 9999)

    assert opened == []


def test_first_run_opens_once_then_never(tmp_path, monkeypatch):
    """Відкриваємо рівно раз: позначка лишається й після перезапусків."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    opened: list[str] = []
    monkeypatch.setattr(first_run.webbrowser, "open", lambda url: opened.append(url))

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
    monkeypatch.setattr(first_run.webbrowser, "open", lambda url: opened.append(url))
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


@pytest.mark.asyncio
async def test_uninstall_refuses_outside_mac_app(monkeypatch):
    """Скриптову інсталяцію й вінду знімають їхні власні інсталятори."""
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", False)

    with pytest.raises(HTTPException) as err:
        await system_routes.trigger_uninstall()

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


def test_uninstall_kill_pattern_targets_only_the_app():
    """pkill мусить влучати саме в бандл. Патерн на кшталт «bhm» зняв би й
    скриптову інсталяцію, якщо людина тримає обидві."""
    cmd = system_routes._build_uninstall_script(purge_data=False)

    assert "BarhandlerManager.app/Contents/MacOS/bhm" in cmd
    assert 'pkill -f "bhm"' not in cmd
