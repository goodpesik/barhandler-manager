"""BH-150 — сторінки майстра установки мусять малюватись як HTML.

Чому це окремий тест: 13.09 власник поставив пакет і на вітальному екрані
побачив ВИХІДНИЙ КОД сторінки — увесь HTML простирадлом. Причина не в CSS і
не в кодуванні: Installer.app вирішує, HTML це чи простий текст, за ПОЧАТКОМ
файлу, а атрибут ``mime-type="text/html"`` у distribution просто ігнорує
(перевірено: пакет без цього атрибута поводився так само). Перед ``<html>``
стояв коментар — і сторінка перетворилась на текст.

Тест дешевий і ловить саме ту помилку, яку легко зробити знову: захотів
пояснити щось у файлі — поставив коментар зверху.
"""

from __future__ import annotations

from pathlib import Path

import pytest

RESOURCES = Path(__file__).resolve().parents[1] / "installers" / "mac-resources"
PAGES = ["welcome.html", "conclusion.html"]


@pytest.mark.parametrize("name", PAGES)
def test_page_starts_with_doctype(name: str) -> None:
    text = (RESOURCES / name).read_text(encoding="utf-8")
    assert text.startswith("<!DOCTYPE html>"), (
        f"{name}: доктайп мусить бути найпершим — інакше майстер покаже розмітку"
    )


@pytest.mark.parametrize("name", PAGES)
def test_page_does_not_pin_text_colour(name: str) -> None:
    """Колір тексту задає Installer — під світлу й темну тему свій.

    Свій ``color`` у CSS дає темне на темному; так само світла плашка під
    ``<code>`` у темній темі ховала текст (видно на тому самому скріншоті).
    """
    css = (RESOURCES / name).read_text(encoding="utf-8")
    body = css[css.index("<style>") : css.index("</style>")]
    assert "color:" not in body, f"{name}: не задавай колір тексту — тема системна"
    assert "background:" not in body, f"{name}: плашка під текстом у темній темі не читається"


def test_legacy_bundle_is_removed_before_any_early_exit() -> None:
    """Стару копію зносимо до розгалужень, а не в гілці «людина за машиною».

    Знайдено ревʼю: у безлюдній гілці (установка по SSH, через MDM або з екрана
    входу) стоїть ``exit 0``, і поки знесення лежало нижче, на таких машинах
    лишались два бандли — два агенти на порт 9999, працює випадковий.
    """
    script = (
        Path(__file__).resolve().parents[1] / "installers" / "mac-postinstall.sh"
    ).read_text(encoding="utf-8")

    # Дивимось лише на КОД: слова «exit 0» трапляються і в коментарях, які
    # саме цю пастку й пояснюють.
    code = [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    removal = next(i for i, line in enumerate(code) if 'rm -rf "$OLD_APP"' in line)
    first_exit = next(i for i, line in enumerate(code) if line == "exit 0")
    assert removal < first_exit, (
        "знесення старого бандла мусить стояти перед першим exit 0"
    )


def test_system_wide_agent_is_written_only_where_nobody_is_logged_in() -> None:
    """Системний агент і користувацький не мусять існувати одночасно.

    Ревʼю назвало це місце неприкритим: postinstall тепер зносить
    ``/Library/LaunchAgents/<label>.plist`` двічі — на початку (разом зі старим
    бандлом) і в гілці «за машиною хтось є». А безлюдна гілка той самий файл
    ПИШЕ. Порядок мусить лишатись таким: спершу знесли, потім та гілка, що
    спрацювала, поклала своє. Якщо запис коли-небудь заїде вище знесення,
    headless-установка лишиться без агента взагалі.
    """
    script = (
        Path(__file__).resolve().parents[1] / "installers" / "mac-postinstall.sh"
    ).read_text(encoding="utf-8")

    code = [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    system_plist = '/Library/LaunchAgents/$LABEL.plist'
    removals = [i for i, line in enumerate(code) if line.startswith("rm -f") and system_plist in line]
    assert len(removals) == 2, "знесення системного агента очікуємо у двох місцях"

    writes = [i for i, line in enumerate(code) if line.startswith('SYS_PLIST=')]
    assert writes, "безлюдна гілка мусить готувати системний агент"
    assert min(removals) < writes[0], (
        "системний агент пишеться раніше, ніж зноситься — headless-установка лишиться без агента"
    )
