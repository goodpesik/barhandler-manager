"""BH-176 — на вінді оновлення качає БРАУЗЕР, а не менеджер.

Ми тричі намагалися зробити це самі: завантажити інсталятор і запустити його
зі свого процесу. Щоразу ламалось по-новому — беззвучний режим і невидимий
діалог SmartScreen (BH-161), дитина, що вмирала разом із нами (BH-174), і
нарешті новий процес, який не міг зайняти порт, бо старий ще тримав його: у
дашборді лишалась стара версія, а кнопка пропонувала оновитись по колу.

Той самий файл, завантажений людиною з браузера й запущений руками, ставиться
без жодної з цих пригод — це й перевірив власник. Тож правило просте: на
замороженій вінда-збірці ми НІЧОГО не запускаємо, а віддаємо адресу файла.
"""

import asyncio
from types import SimpleNamespace

import pytest

from src.routes import system as system_routes


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


def _as_windows_exe(monkeypatch):
    monkeypatch.setattr(system_routes, "IS_WIN", True)
    monkeypatch.setattr(system_routes, "FROZEN", True)
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", False)


def test_the_manager_starts_nothing_on_windows(monkeypatch):
    """Головне: жодного процесу. Саме запуск інсталятора з-під менеджера й був
    джерелом усіх трьох попередніх поломок."""
    _as_windows_exe(monkeypatch)

    def _boom(*a, **kw):  # pragma: no cover — має не викликатись
        raise AssertionError("менеджер знову намагається запустити інсталятор")

    monkeypatch.setattr(system_routes.subprocess, "Popen", _boom)

    result = asyncio.run(system_routes.trigger_update(_request()))
    assert result["started"] is False
    assert result["download_url"] == system_routes.WIN_INSTALLER_URL


def test_the_answer_names_the_installer_people_actually_download(monkeypatch):
    """Адреса має вести на той самий інсталятор, який людина качає руками з
    релізу, — інакше кнопка й ручний шлях розійдуться версіями."""
    assert system_routes.WIN_INSTALLER_URL.endswith("/device-handler-setup.exe")
    assert "releases/latest/download" in system_routes.WIN_INSTALLER_URL


def test_the_dashboard_is_told_where_to_download(monkeypatch):
    _as_windows_exe(monkeypatch)
    payload = asyncio.run(system_routes.get_version())
    assert payload["update_download_url"] == system_routes.WIN_INSTALLER_URL


def test_other_systems_are_not_sent_to_a_download(monkeypatch):
    """Мак і скриптова інсталяція оновлюються самі — там адреси немає, інакше
    дашборд запропонував би людині вінда-інсталятор."""
    monkeypatch.setattr(system_routes, "IS_WIN", False)
    monkeypatch.setattr(system_routes, "FROZEN", True)
    payload = asyncio.run(system_routes.get_version())
    assert payload["update_download_url"] is None


def test_a_payment_in_flight_still_blocks_the_button(monkeypatch):
    """BH-164 лишається: поки йде оплата карткою, людині не віддаємо навіть
    посилання — інсталятор вона відкриє за хвилину й знесе менеджер посеред
    обміну з терміналом."""
    _as_windows_exe(monkeypatch)
    monkeypatch.setattr(
        system_routes, "busy_refusal", lambda *a, **kw: {"message": "зайнято"}
    )
    with pytest.raises(Exception) as err:
        asyncio.run(system_routes.trigger_update(_request()))
    assert getattr(err.value, "status_code", None) == 409
