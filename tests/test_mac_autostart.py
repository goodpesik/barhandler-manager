"""Автозапуск мак-збірки: BH-148.

Один файл LaunchAgent і один лейбл пише і скриптова інсталяція
(``installers/install.sh``), і застосунок із .dmg. Обидва слухають той самий
порт 9999, тож автозапуск мусить бути ОДИН — і головне тут не «зареєструвати»,
а НЕ ЗАБРАТИ чужий: одне відкриття .dmg «на пробу» не має мовчки підміняти
людині те, що стартує при вході в систему.
"""

import plistlib
import sys
from pathlib import Path

import pytest

from src.services import mac_autostart


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Своя домівка на тест: Path.home() на posix читає $HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _plist(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / "com.goodpesik.barhandler-manager.plist"


def _as_frozen_app(monkeypatch, exe: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", str(exe))


def test_noop_when_not_frozen(home, monkeypatch):
    """Із джерел (і на вінді) не пишемо нічого — інакше розробницький запуск
    підмінив би собою робочу інсталяцію."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")

    mac_autostart.ensure_launch_agent()

    assert not _plist(home).exists()


def test_noop_when_not_darwin(home, monkeypatch):
    exe = home / "Applications" / "Device Handler.app" / "Contents" / "MacOS" / "bhm"
    _as_frozen_app(monkeypatch, exe)
    monkeypatch.setattr(sys, "platform", "win32")

    mac_autostart.ensure_launch_agent()

    assert not _plist(home).exists()


def test_registers_when_missing(home, monkeypatch):
    exe = home / "Applications" / "Device Handler.app" / "Contents" / "MacOS" / "bhm"
    exe.parent.mkdir(parents=True)
    exe.touch()
    _as_frozen_app(monkeypatch, exe)

    mac_autostart.ensure_launch_agent()

    data = plistlib.loads(_plist(home).read_bytes())
    assert data["ProgramArguments"] == [str(exe.resolve())]
    # Обидва прапорці — суть автозапуску: піднятись на вході і не вмирати.
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True


def test_keeps_script_install_autostart(home, monkeypatch):
    """Головне в цьому файлі. plist скриптової інсталяції — два аргументи
    (інтерпретатор + main.py). Такий ми не чіпаємо."""
    script_args = [
        str(home / ".barhandler-manager" / ".venv" / "bin" / "python"),
        str(home / ".barhandler-manager" / "main.py"),
    ]
    plist = _plist(home)
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"Label": "x", "ProgramArguments": script_args}))

    exe = home / "Applications" / "Device Handler.app" / "Contents" / "MacOS" / "bhm"
    exe.parent.mkdir(parents=True)
    exe.touch()
    _as_frozen_app(monkeypatch, exe)

    mac_autostart.ensure_launch_agent()

    assert plistlib.loads(plist.read_bytes())["ProgramArguments"] == script_args


def test_retargets_another_app_copy(home, monkeypatch):
    """А ось перенесену/переставлену .app-копію перенаправляємо на себе:
    інакше після переїзду в /Applications автозапуск указував би в нікуди."""
    old = home / "Downloads" / "Device Handler.app" / "Contents" / "MacOS" / "bhm"
    plist = _plist(home)
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"Label": "x", "ProgramArguments": [str(old)]}))

    exe = home / "Applications" / "Device Handler.app" / "Contents" / "MacOS" / "bhm"
    exe.parent.mkdir(parents=True)
    exe.touch()
    _as_frozen_app(monkeypatch, exe)

    mac_autostart.ensure_launch_agent()

    assert plistlib.loads(plist.read_bytes())["ProgramArguments"] == [str(exe.resolve())]


def test_never_raises_on_broken_plist(home, monkeypatch):
    """Реєстрація автозапуску не має права завалити старт сервера."""
    plist = _plist(home)
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"\x00 not a plist")

    exe = home / "Applications" / "Device Handler.app" / "Contents" / "MacOS" / "bhm"
    exe.parent.mkdir(parents=True)
    exe.touch()
    _as_frozen_app(monkeypatch, exe)

    mac_autostart.ensure_launch_agent()  # не кидає

    assert plistlib.loads(plist.read_bytes())["ProgramArguments"] == [str(exe.resolve())]


# ── реєстрація через SMAppService (BH-150) ─────────────────────────────────
#
# Це той шлях, який дає системі впізнати нас: у «Автозапуск і розширення»
# зʼявляється «Device Handler», а не «ПЗ Maksym Levynets» — команда з
# сертифіката. Старий спосіб (plist у домівці) лишається запасним, і саме його
# збереження тут найважливіше: якщо SMAppService колись відмовить, автозапуск
# мусить працювати далі, хоч і з чужою назвою.

import types


class _FakeService:
    def __init__(self, status=3, register_ok=True):
        self._status = status
        self._register_ok = register_ok
        self.registered = False

    def status(self):
        return self._status

    def registerAndReturnError_(self, _err):
        self.registered = True
        if self._register_ok:
            return True, None
        return False, "відмова"


def _fake_sm(monkeypatch, service):
    module = types.SimpleNamespace(
        SMAppService=types.SimpleNamespace(
            agentServiceWithPlistName_=lambda name: service,
        ),
    )
    monkeypatch.setitem(sys.modules, "ServiceManagement", module)


def _bundle_with_agent(home: Path) -> Path:
    exe = home / "Applications" / "Device Handler.app" / "Contents" / "MacOS" / "bhm"
    exe.parent.mkdir(parents=True)
    exe.touch()
    agents = exe.parent.parent / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / mac_autostart._BUNDLED_AGENT).write_text("<plist/>")
    return exe


def test_service_management_skipped_without_bundled_agent(home, monkeypatch):
    """Немає плиста в бандлі — нема чого реєструвати; йдемо старим шляхом."""
    exe = home / "Applications" / "Device Handler.app" / "Contents" / "MacOS" / "bhm"
    exe.parent.mkdir(parents=True)
    exe.touch()
    _as_frozen_app(monkeypatch, exe)

    assert mac_autostart._register_via_service_management() is False


def test_service_management_registers_and_skips_legacy_plist(home, monkeypatch):
    """Успішна реєстрація означає, що plist у домівці НЕ пишеться: два агенти
    з одним лейблом воювали б за порт 9999."""
    exe = _bundle_with_agent(home)
    _as_frozen_app(monkeypatch, exe)
    service = _FakeService(status=3, register_ok=True)
    _fake_sm(monkeypatch, service)

    mac_autostart.ensure_launch_agent()

    assert service.registered
    assert not _plist(home).exists()


def test_service_management_does_not_register_twice(home, monkeypatch):
    """status 1 = enabled: агент уже зареєстрований, повторна реєстрація зайва."""
    exe = _bundle_with_agent(home)
    _as_frozen_app(monkeypatch, exe)
    service = _FakeService(status=1)
    _fake_sm(monkeypatch, service)

    assert mac_autostart._register_via_service_management() is True
    assert not service.registered


def test_falls_back_to_legacy_plist_when_registration_refused(home, monkeypatch):
    """Головне в цьому блоці. SMAppService відмовив — автозапуск усе одно
    мусить працювати, хай і з назвою команди в системних налаштуваннях."""
    exe = _bundle_with_agent(home)
    _as_frozen_app(monkeypatch, exe)
    _fake_sm(monkeypatch, _FakeService(status=3, register_ok=False))

    mac_autostart.ensure_launch_agent()

    assert _plist(home).exists(), "відмова SMAppService лишила машину без автозапуску"


def test_falls_back_when_pyobjc_missing(home, monkeypatch):
    """Збірка без pyobjc (чи не-мак збірка pyinstaller) — не привід лишитись
    без автозапуску."""
    exe = _bundle_with_agent(home)
    _as_frozen_app(monkeypatch, exe)
    monkeypatch.setitem(sys.modules, "ServiceManagement", None)

    mac_autostart.ensure_launch_agent()

    assert _plist(home).exists()
