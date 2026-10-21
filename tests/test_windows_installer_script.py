"""Інсталятор вінди (`installers/barhandler-setup.iss`) як ТЕКСТ.

Inno-скрипт на маку не збереш, тож перевіряємо те, що можна перевірити без
Windows: які саме процеси інсталятор знімає перед установкою й при видаленні.

BH-161. Реліз публікує той самий бінарник під двома іменами — `bhm.exe`
(так його кладе інсталятор) і `device-handler.exe` (standalone-актив із
v0.4.0, і саме його запускає людина, яка завантажила exe напряму). Доти
`taskkill` знав лише перше ім'я: установка проходила «успішно», а порт 9999 і
файл лишалась тримати стара копія. Це друге пояснення того, чому в клієнта
`pid` менеджера не змінювався між двома спробами оновлення.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ISS = Path(__file__).resolve().parent.parent / "installers" / "barhandler-setup.iss"

# Обидва імені — НАШІ, і обидва можуть бути живим процесом менеджера.
BOTH_EXE_NAMES = ("bhm.exe", "device-handler.exe")


@pytest.fixture(scope="module")
def iss() -> str:
    return ISS.read_text(encoding="utf-8")


def _defined(iss: str, name: str) -> str:
    """Значення `#define <name> "..."` зі скрипта."""
    m = re.search(rf'^#define\s+{re.escape(name)}\s+"([^"]+)"', iss, re.MULTILINE)
    assert m, f"немає #define {name}"
    return m.group(1)


def _taskkill_targets(iss: str, where: str | None = None) -> list[str]:
    """Усі імена образів, у які стріляє `taskkill /F /IM`, з розкритими
    константами `{#MyAppExe*}`.

    `where` — фрагмент скрипта, у якому шукати; самі `#define` при цьому
    беруться з ПОВНОГО тексту, бо у фрагменті їх немає.
    """
    raw = re.findall(r"taskkill /F /IM (\S+?)(?:'|\"|\s|$)", where if where is not None else iss)
    resolved = []
    for token in raw:
        m = re.fullmatch(r"\{#(\w+)\}", token)
        resolved.append(_defined(iss, m.group(1)) if m else token)
    return resolved


def test_both_exe_names_are_declared(iss: str) -> None:
    """Імена мусять бути константами, а не розсипаними рядками: наступне
    місце, яке їх забуде, — це знову той самий баг."""
    assert _defined(iss, "MyAppExeName") == "bhm.exe"
    assert _defined(iss, "MyAppExeAltName") == "device-handler.exe"


def test_the_installer_kills_both_names_before_installing(iss: str) -> None:
    """Суть фікса. Жива стара копія тримає порт 9999 і власний файл —
    установка «вдається», а працює далі вона."""
    body = iss.split("procedure CleanPrevious", 1)[1]
    targets = _taskkill_targets(iss, body)
    for name in BOTH_EXE_NAMES:
        assert name in targets, f"{name} не знімається перед установкою"


def test_the_uninstaller_kills_both_names(iss: str) -> None:
    """Видалення, що лишає процес живим, лишає й порт зайнятим."""
    section = iss.split("[UninstallRun]", 1)[1].split("\n[", 1)[0]
    targets = _taskkill_targets(iss, section)
    for name in BOTH_EXE_NAMES:
        assert name in targets, f"{name} не знімається при видаленні"


def test_no_place_kills_only_one_of_the_two(iss: str) -> None:
    """Перевірка на КІЛЬКІСТЬ, не лише на наявність.

    Місць, що знімають процес, два — перед установкою і при видаленні. Якщо
    десь дописати третє й забути друге ім'я, попередні тести це проґавлять:
    обидва імені в скрипті вже будуть. Тут падає саме такий випадок.
    """
    targets = _taskkill_targets(iss)
    for name in BOTH_EXE_NAMES:
        assert targets.count(name) == 2, (
            f"{name} згадано в {targets.count(name)} taskkill, а місць два "
            f"(установка + видалення): {targets}"
        )


# BH-190 — the manager starts at system start, not at the installing user's logon.


def _boot_task_script(iss: str) -> str:
    return iss.split("function RegisterBootTask", 1)[1].split("\nend;", 1)[0]


def test_the_boot_task_starts_at_boot_without_a_logon(iss: str) -> None:
    script = _boot_task_script(iss)
    assert "New-ScheduledTaskTrigger -AtStartup" in script
    assert "-AtLogOn" not in script
    # S4U runs it whether the user is logged on or not; Interactive would not.
    assert "-LogonType S4U" in script


def test_the_boot_task_is_never_stopped_by_a_time_limit(iss: str) -> None:
    """Without it the scheduler's default stops the task after 72 hours."""
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in _boot_task_script(iss)


def test_the_startup_shortcut_is_only_the_fallback(iss: str) -> None:
    """A shortcut made unconditionally would keep the start bound to the logon
    and start a second copy beside the task."""
    icons = iss.split("[Icons]", 1)[1].split("\n[", 1)[0]
    assert "{userstartup}" not in "\n".join(
        line for line in icons.splitlines() if not line.lstrip().startswith(";")
    )
    step = iss.split("procedure CurStepChanged", 1)[1].split("\nend;", 1)[0]
    assert step.index("MakeStartupShortcut") > step.index("else")


def test_the_uninstaller_removes_the_task_and_the_shortcut(iss: str) -> None:
    uninstall = iss.split("procedure CurUninstallStepChanged", 1)[1]
    assert "Unregister-ScheduledTask -TaskName ''{#BootTaskName}''" in uninstall
    section = iss.split("[UninstallDelete]", 1)[1].split("\n[", 1)[0]
    assert "{userstartup}\\{#MyAppName}.lnk" in section
