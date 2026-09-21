"""Версію читає ОДИН помічник, і оновлення пише її в лог.

Після BH-174 лог оновлення показує, що спроба почалась. Але з якої версії — ні,
а саме це питання й виникає першим, коли перевіряєш, чи оновлення доїхало:
«воно ще старе чи вже нове». Той самий рядок читання `VERSION` жив у чотирьох
місцях із двома різними глибинами шляху й двома різними запасними значеннями.
"""

from pathlib import Path

import pytest

from src.version import installed_version, VERSION_FILE


SRC = Path(__file__).resolve().parent.parent / "src"


def test_the_version_file_sits_next_to_the_code_not_the_cwd():
    """У exe робоча тека довільна, тож шлях рахується від коду."""
    assert VERSION_FILE.name == "VERSION"
    assert VERSION_FILE.parent == SRC.parent


def test_a_missing_version_file_is_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.version.VERSION_FILE", tmp_path / "нема", raising=True
    )
    assert installed_version() == "0.0.0"
    assert installed_version("unknown") == "unknown"


def test_an_empty_version_file_falls_back_too(monkeypatch, tmp_path):
    empty = tmp_path / "VERSION"
    empty.write_text("\n")
    monkeypatch.setattr("src.version.VERSION_FILE", empty, raising=True)
    assert installed_version("unknown") == "unknown"


def test_the_version_is_read_in_one_place_only():
    """Кількість теж фіксуємо: новий читач має пройти повз цей тест свідомо."""
    readers = [
        path
        for path in SRC.rglob("*.py")
        if 'VERSION"' in path.read_text(encoding="utf-8")
    ]
    assert [p.name for p in readers] == ["version.py"], (
        "версію читає лише src/version.py — решта кличе installed_version()"
    )


@pytest.mark.asyncio
async def test_the_update_log_header_names_the_version(monkeypatch, tmp_path):
    """Рядок у лозі має казати, ЗВІДКИ оновлюємось: інакше після оновлення
    незрозуміло, чи спрацювало воно, чи інсталятор поставив те саме."""
    from src.routes import system as system_routes

    log = tmp_path / "update.log"
    monkeypatch.setattr(system_routes, "_UPDATE_LOG", log, raising=True)
    monkeypatch.setattr(system_routes, "_INSTALL_DIR", tmp_path, raising=True)
    monkeypatch.setattr(
        system_routes, "installed_version", lambda *a: "9.9.9", raising=True
    )

    class _Proc:
        pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(
        system_routes.subprocess, "Popen", lambda *a, **k: _Proc()
    )
    monkeypatch.setattr(
        system_routes, "_build_update_argv", lambda: ([], "cmd")
    )
    monkeypatch.setattr(system_routes, "busy_refusal", lambda *a, **k: None)

    class _Req:
        app = None

    await system_routes.trigger_update(_Req())

    assert "version=9.9.9" in log.read_text()
