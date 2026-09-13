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

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RESOURCES = REPO / "installers" / "mac-resources"
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


def _code_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_pkg_build_uses_the_component_description() -> None:
    """`pkgbuild` мусить збирати пакет саме з нашим описом компонента.

    Попередня версія цього тесту шукала рядки в усьому файлі, разом із
    коментарями — знайдено ревʼю: так вона проходила б і тоді, коли робочі
    рядки закомічено, а пояснення лишилось. Тому дивимось лише на КОД і
    перевіряємо звʼязок: опис складається, і той самий файл іде в pkgbuild.
    """
    code = _code_lines(REPO / "scripts" / "mac_build_pkg.sh")
    joined = "\n".join(code)

    assert "mac_component_plist.sh" in joined, "опис компонента ніхто не складає"
    assert "--component-plist" in joined, "опис компонента не передано в pkgbuild"

    made = next(i for i, line in enumerate(code) if "mac_component_plist.sh" in line)
    used = next(i for i, line in enumerate(code) if "--component-plist" in line)
    assert made < used, "опис передають у pkgbuild раніше, ніж він створений"


def test_component_script_clears_both_flags_and_checks_the_result() -> None:
    """У описі знімаємо ОБА прапорці й переконуємось, що це справді сталося.

    `BundleIsRelocatable` — щоб пакет не поставився поверх копії, знайденої
    Spotlight (саме так вміст поїхав у теку `dist/`). `BundleIsVersionChecked`
    — щоб копія з більшим номером версії не блокувала розпакування тихо:
    скрипти тоді відпрацюють, файл на місці, усі перевірки задоволені, а вміст
    із пакета не поставився (знайдено ревʼю).
    """
    code = "\n".join(_code_lines(REPO / "scripts" / "mac_component_plist.sh"))

    for key in ("BundleIsRelocatable", "BundleIsVersionChecked"):
        assert f"Set :0:$key false" in code or f"Set :0:{key} false" in code, key
        assert key in code

    assert "Print :0:$key" in code or "Print :0:" in code, (
        "результат не перечитують — PlistBuddy на помилці часто повертає 0"
    )
    assert "Print :1" in code, "ніхто не перевіряє, що бандл у пакеті один"
    assert "ChildBundles" in code, "вкладені бандли лишились без перевірки"


@pytest.mark.skipif(sys.platform != "darwin", reason="pkgbuild і PlistBuddy є лише на macOS")
@pytest.mark.skipif(shutil.which("pkgbuild") is None, reason="немає pkgbuild")
def test_built_package_relocates_nothing(tmp_path: Path) -> None:
    """Справжня перевірка механізму: збираємо пакет і дивимось у PackageInfo.

    Текстові перевірки вище ловлять лише видалені рядки. Ця — поводження: із
    типовим описом у PackageInfo лежить `<relocate><bundle …></relocate>`, і
    саме він відправив вміст у чужу теку. Після нашого опису `<relocate>`
    мусить бути порожній.
    """
    app = tmp_path / "root" / "Applications" / "Device Handler.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "bhm").write_text("#!/bin/sh\n")
    (app / "bhm").chmod(0o755)
    (app.parent / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.goodpesik.barhandler-manager",
                "CFBundleName": "Device Handler",
                "CFBundleExecutable": "bhm",
                "CFBundleShortVersionString": "9.9.9",
                "CFBundlePackageType": "APPL",
            }
        )
    )

    component = tmp_path / "component.plist"
    subprocess.run(
        ["bash", str(REPO / "scripts" / "mac_component_plist.sh"), str(tmp_path / "root"), str(component)],
        check=True,
        capture_output=True,
    )
    described = plistlib.loads(component.read_bytes())
    assert described[0]["BundleIsRelocatable"] is False
    assert described[0]["BundleIsVersionChecked"] is False

    pkg = tmp_path / "app.pkg"
    subprocess.run(
        [
            "pkgbuild",
            "--root", str(tmp_path / "root"),
            "--component-plist", str(component),
            "--identifier", "com.goodpesik.barhandler-manager.app",
            "--version", "9.9.9",
            "--install-location", "/",
            str(pkg),
        ],
        check=True,
        capture_output=True,
    )

    expanded = tmp_path / "expanded"
    subprocess.run(["pkgutil", "--expand", str(pkg), str(expanded)], check=True, capture_output=True)
    info = (expanded / "PackageInfo").read_text(encoding="utf-8")

    relocate = info[info.index("<relocate") : info.index(">", info.index("<relocate")) + 1]
    if not relocate.endswith("/>"):
        block = info[info.index("<relocate>") : info.index("</relocate>")]
        assert "<bundle" not in block, f"бандл лишився переміщуваним: {block}"


def test_legacy_bundle_is_removed_only_after_the_new_one_is_confirmed() -> None:
    """Стару копію зносимо ЛИШЕ коли нова вже на місці.

    Знайдено ревʼю: у зворотному порядку невдале розпакування лишало машину
    взагалі без менеджера — гірше, ніж було до установки. Помилку показати
    мало, її треба не робити.
    """
    code = _code_lines(REPO / "installers" / "mac-postinstall.sh")
    check = next(i for i, line in enumerate(code) if line.startswith("if [ ! -x \"$APP\" ]"))
    removal = next(i for i, line in enumerate(code) if 'rm -rf "$OLD_APP"' in line)
    assert check < removal, "перевірка нової копії мусить стояти перед знесенням старої"


def test_postinstall_respects_the_target_volume() -> None:
    """Шляхи беруться від тома установки, а не жорстко від «/».

    `installer -target /Volumes/X` (розгортання образу, MDM) кладе вміст на
    інший том. Із жорстким /Applications перевірка вище не знайшла б застосунку
    й завалила б установку без причини (знайдено ревʼю).
    """
    code = "\n".join(_code_lines(REPO / "installers" / "mac-postinstall.sh"))
    assert 'TARGET="${3:-/}"' in code, "том установки ($3) не читається"
    assert 'APP="$TARGET/Applications' in code, "шлях застосунку не залежить від тома"
    assert 'OLD_APP="$TARGET/Applications' in code, "шлях старої копії не залежить від тома"
