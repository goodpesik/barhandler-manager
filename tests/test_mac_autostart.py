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
    exe = home / "Applications" / "BarhandlerManager.app" / "Contents" / "MacOS" / "bhm"
    _as_frozen_app(monkeypatch, exe)
    monkeypatch.setattr(sys, "platform", "win32")

    mac_autostart.ensure_launch_agent()

    assert not _plist(home).exists()


def test_registers_when_missing(home, monkeypatch):
    exe = home / "Applications" / "BarhandlerManager.app" / "Contents" / "MacOS" / "bhm"
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

    exe = home / "Applications" / "BarhandlerManager.app" / "Contents" / "MacOS" / "bhm"
    exe.parent.mkdir(parents=True)
    exe.touch()
    _as_frozen_app(monkeypatch, exe)

    mac_autostart.ensure_launch_agent()

    assert plistlib.loads(plist.read_bytes())["ProgramArguments"] == script_args


def test_retargets_another_app_copy(home, monkeypatch):
    """А ось перенесену/переставлену .app-копію перенаправляємо на себе:
    інакше після переїзду в /Applications автозапуск указував би в нікуди."""
    old = home / "Downloads" / "BarhandlerManager.app" / "Contents" / "MacOS" / "bhm"
    plist = _plist(home)
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"Label": "x", "ProgramArguments": [str(old)]}))

    exe = home / "Applications" / "BarhandlerManager.app" / "Contents" / "MacOS" / "bhm"
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

    exe = home / "Applications" / "BarhandlerManager.app" / "Contents" / "MacOS" / "bhm"
    exe.parent.mkdir(parents=True)
    exe.touch()
    _as_frozen_app(monkeypatch, exe)

    mac_autostart.ensure_launch_agent()  # не кидає

    assert plistlib.loads(plist.read_bytes())["ProgramArguments"] == [str(exe.resolve())]
