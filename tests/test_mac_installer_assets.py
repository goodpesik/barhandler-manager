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
import re
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
    code = _code_lines(REPO / "scripts" / "mac_component_plist.sh")

    # Дивимось саме на РЯДОК, що перелічує прапорці. Попередня версія шукала
    # назву будь-де в коді — знайдено другим колом ревʼю: так вона проходила б
    # і тоді, коли прапорець прибрали з переліку, а назва лишилась у якомусь
    # повідомленні.
    listing = [line for line in code if line.startswith("for key in ")]
    assert len(listing) == 1, "очікуємо один перелік прапорців"
    for key in ("BundleIsRelocatable", "BundleIsVersionChecked"):
        assert key in listing[0], f"{key} не знімається"

    joined = "\n".join(code)
    assert "Set :0:$key false" in joined, "прапорці не знімаються в тому переліку"
    assert "Print :0:$key" in joined, (
        "результат не перечитують — а PlistBuddy на помилці не завжди падає"
    )
    assert "Print :0\"" in joined or 'Print :0"' in joined, (
        "немає перевірки, що бандл у пакеті взагалі є"
    )
    assert "Print :1" in joined, "ніхто не перевіряє, що бандл у пакеті один"
    assert "ChildBundles" in joined, "вкладені бандли лишились без перевірки"


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


def test_install_to_another_volume_really_registers_autostart(tmp_path: Path) -> None:
    """Прогоняємо postinstall із чужим томом і дивимось на результат.

    Знайдено другим колом ревʼю: у цій гілці стояв лише ``exit 0`` з
    приміткою «застосунок зареєструє автозапуск сам». Неправда:
    ``ensure_launch_agent()`` виконується лише тоді, коли застосунок ХТОСЬ
    запустив, а в безлюдному розгортанні запускати нікому. Тепер гілка кладе
    агент для всіх на той том — і цей тест перевіряє саме файл, а не текст
    скрипта.
    """
    target = tmp_path / "Volumes" / "Macintosh HD 2"
    app = target / "Applications" / "Device Handler.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "bhm").write_text("#!/bin/sh\n")
    (app / "bhm").chmod(0o755)

    done = subprocess.run(
        ["bash", str(REPO / "installers" / "mac-postinstall.sh"), "pkg", "dest", str(target)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr

    plist = target / "Library" / "LaunchAgents" / "com.goodpesik.barhandler-manager.plist"
    assert plist.exists(), f"агент не покладено:\n{done.stdout}\n{done.stderr}"

    body = plist.read_text(encoding="utf-8")
    # Шлях у plist — від кореня ТІЄЇ системи: після завантаження той том буде "/".
    assert "<string>/Applications/Device Handler.app/Contents/MacOS/bhm</string>" in body
    assert str(target) not in body, "шлях із тимчасової точки монтування в plist не годиться"


def test_missing_payload_fails_the_install(tmp_path: Path) -> None:
    """Немає застосунку — postinstall мусить завалити установку, а не звітувати успіх."""
    target = tmp_path / "empty"
    (target / "Applications").mkdir(parents=True)

    done = subprocess.run(
        ["bash", str(REPO / "installers" / "mac-postinstall.sh"), "pkg", "dest", str(target)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 1, f"установка мовчки «вдалася»:\n{done.stdout}"
    assert "установка неповна" in done.stdout + done.stderr


def test_installers_do_not_spawn_a_second_copy() -> None:
    """Фолбек «підняти вручну» не має плодити другу копію на порті 9999.

    13.09.2026 у клієнта на планшеті працювали дві копії: одна тримала порт,
    другу runit піднімав по колу кожні 3 секунди з `address already in use`.
    Фронт то бачив менеджер, то ні, платіж падав із «Manager terminal
    unavailable». Причина — в Android-скрипті ми чекали на /health ОДНУ
    секунду, а старт uvicorn на планшеті займає 4+ секунди, тож друга копія
    запускалась щоразу.
    """
    for name in ("install-android.sh", "install.sh"):
        script = (REPO / "installers" / name).read_text(encoding="utf-8")
        # Саме РЯДОК запуску, а не слово «nohup» у коментарях вище.
        # Місць запуску другої копії може бути кілька — під захистом мусять
        # бути ВСІ. Знайдено цим самим тестом: в install-android.sh їх два, і
        # спершу я поправив лише те, що нижче.
        # Патерн НЕ чіпляється до лапок: у згенерованих скриптах шлях
        # підставляється без них. Знайдено ревʼю — саме через лапки тест не
        # бачив третього місця запуску в install-android.sh і проходив із
        # живою вадою.
        starts = [
            m.start()
            for m in re.finditer(r'nohup\s+"?\$INSTALL_DIR/\.venv/bin/python"?', script)
        ]
        assert starts, f"{name}: не знайшов запуску через nohup"
        for nth, pos in enumerate(starts, 1):
            # Запобіжник мусить стояти ПОРУЧ із цим запуском, а не будь-де у
            # файлі: перевірка з іншої гілки наступну не рятує.
            nearby = script[max(0, pos - 900) : pos]
            # Приймаємо лише ІМЕНОВАНИЙ хелпер — `manager_alive` в інсталяторі
            # або `alive()` у згенерованому скрипті. Голий `pgrep` більше не
            # годиться: знайдено другим колом ревʼю, що без фолбеку на ps він
            # там, де немає procps, тихо стає «не працює» — тобто вада
            # вертається, а тест її не бачить.
            guarded = "manager_alive" in nearby or "if alive;" in nearby
            assert guarded, f"{name}: запуск №{nth} без перевірки, чи процес уже є"

    android = (REPO / "installers" / "install-android.sh").read_text(encoding="utf-8")
    # У скрипті ДВА таких фолбеки — рання гілка «встановлено, але не працює» і
    # основна після установки. Перевіряємо саме основну: беремо останній.
    before_spawn = android[: android.rindex("spawning manager directly")]
    assert "seq 1 30" in before_spawn, (
        "install-android.sh: на підняття дають замало часу — саме на цьому "
        "зʼявилась друга копія"
    )
    assert "sleep 1\nif ! curl" not in android, "лишився старий однесекундний фолбек"


def test_every_spawn_site_is_counted() -> None:
    """Скільком місцям запуску ми довіряємо — стільком і маємо давати перевірку.

    Тест вище обходить знайдені місця; цей стежить, щоб їх не стало більше
    непоміченими. Знайдено ревʼю: перша версія бачила два з трьох, бо чіплялась
    до лапок, і проходила при живій ваді.
    """
    expected = {"install-android.sh": 3, "install.sh": 2}
    for name, count in expected.items():
        script = (REPO / "installers" / name).read_text(encoding="utf-8")
        found = re.findall(r'nohup\s+"?\$INSTALL_DIR/\.venv/bin/python"?', script)
        assert len(found) == count, (
            f"{name}: місць запуску {len(found)}, очікували {count} — "
            "перевір, чи нове місце захищене, і онови число"
        )


def test_android_verifies_the_result_outside_the_spawn_branch() -> None:
    """Перевірка версії не має лежати всередині гілки «ми запускали самі».

    Знайдено ревʼю: доти вона була під `if`, і гілка «процес живий» його
    проминала — скрипт друкував успіх і виходив 0, навіть якщо той процес
    завис і /health не відповів ні разу.
    """
    code = _code_lines(REPO / "installers" / "install-android.sh")
    spawn_if = next(i for i, line in enumerate(code) if line.startswith('if [ "$NEED_SPAWN"'))
    fi_after = next(i for i, line in enumerate(code[spawn_if:], spawn_if) if line == "fi")
    version_check = next(
        i for i, line in enumerate(code) if line.startswith('EXPECTED_VERSION=')
    )
    assert version_check > fi_after, (
        "перевірка версії лежить у гілці запуску — гілка «процес живий» її проминає"
    )


def test_termux_installs_procps() -> None:
    """Без pgrep/pkill усі запобіжники тихо деградують у «не працює».

    Термукс не завжди має procps у базі, а на ньому тримається і знесення
    старого процесу, і перевірка перед запуском другої копії.
    """
    script = (REPO / "installers" / "install-android.sh").read_text(encoding="utf-8")
    pkg_install = script[script.index("pkg install -y") :][:400]
    assert "procps" in pkg_install, "procps не ставиться — pgrep може бути відсутній"


def test_generated_scripts_survive_a_missing_pgrep() -> None:
    """У згенерованих start.sh перевірка мусить мати фолбек на `ps`.

    `pgrep`/`pkill` — це пакет procps, якого в Термуксі не завжди є. Без
    фолбеку перевірка «чи процес уже працює» там тихо стає «не працює», і
    друга копія запускається знову — та сама вада BH-151, лише без жодного
    повідомлення. Знайдено другим колом ревʼю: спершу в обох згенерованих
    скриптах стояв голий `pgrep`.
    """
    for name in ("install-android.sh", "install.sh"):
        script = (REPO / "installers" / name).read_text(encoding="utf-8")
        helper = script[script.index("alive() {") : script.index("if alive;")]
        assert "command -v pgrep" in helper, f"{name}: немає перевірки наявності pgrep"
        assert "ps -A -o args=" in helper, f"{name}: немає фолбеку на ps"


def test_install_failure_is_judged_by_version_not_by_anyone_answering() -> None:
    """Попередження про невдачу — за версією, а не за «хтось відповідає».

    Стара копія, що не завершилась і тримає порт 9999, теж відповідає на
    /health. Доти умова була `! is_running`, тож найгірший випадок — нова
    версія не піднялась, працює стара — проходив без попередження: скрипт
    друкував успіх і виходив 0 (знайдено другим колом ревʼю).
    """
    for name in ("install-android.sh", "install.sh"):
        code = "\n".join(_code_lines(REPO / "installers" / name))
        assert "VERSION_OK=1" in code, f"{name}: успіх перевірки версії ніде не фіксується"
        assert "! is_running; then" not in code, (
            f"{name}: невдача все ще визначається через «хтось відповідає на /health»"
        )


# ─── BH-155 ──────────────────────────────────────────────────────────────────
#
# Оновлення 0.5.3 → 0.5.7 на живій машині лишило її БЕЗ менеджера: postinstall
# записав агент, а підняти його не зміг («bootstrap не вдався» у
# /var/log/install.log) — бо працює від root, а агент живе в графічній сесії
# користувача. Той самий рядок з оболонки користувача проходив із кодом 0.


def test_agent_is_started_inside_the_user_session() -> None:
    """Агент піднімаємо через `launchctl asuser`, а не напряму з root."""
    code = "\n".join(_code_lines(REPO / "installers" / "mac-postinstall.sh"))

    assert 'launchctl asuser "$CONSOLE_UID" launchctl' in code, (
        "з root-контексту `launchctl bootstrap gui/<uid>` не проходить — "
        "саме через це оновлення гасило менеджер"
    )
    assert "bootstrap" in code


def test_postinstall_waits_for_the_old_agent_to_go() -> None:
    """Після bootout чекаємо, поки служба справді зникне.

    Без очікування наступний bootstrap ловить «service already loaded» і не
    робить нічого, а працює далі попередня копія — тобто оновлення не
    оновлює.
    """
    code = _code_lines(REPO / "installers" / "mac-postinstall.sh")
    bootout = next(i for i, line in enumerate(code) if "bootout" in line)
    wait = next(
        (i for i, line in enumerate(code) if line.startswith("as_user print")), None
    )
    assert wait is not None, "немає перевірки, чи служба зникла"
    assert bootout < wait, "очікування мусить стояти ПІСЛЯ bootout"


def test_postinstall_verifies_health_before_claiming_success() -> None:
    """Не звітуємо успіх без перевірки.

    Доти скрипт писав «менеджер стартує при наступному вході» — і це була
    напівправда: до перезаходу менеджера не було взагалі, а установка
    виглядала успішною.
    """
    code = "\n".join(_code_lines(REPO / "installers" / "mac-postinstall.sh"))

    assert "localhost:9999/health" in code, "успіх не перевіряється жодним запитом"
    assert "НЕ ВДАЛОСЯ підняти менеджер" in code, (
        "немає честного повідомлення про невдачу"
    )


def test_installer_compares_versions_instead_of_just_existence() -> None:
    """Інсталятор мусить казати про версії, а не «вже встановлено».

    Слова власника: «повинен би версії перевіряти і пропонувати оновлення, а
    не казати шо вже встановлено».
    """
    dist = (REPO / "installers" / "mac-distribution.xml").read_text(encoding="utf-8")

    assert "check_not_installed" not in dist, "лишилась стара перевірка на наявність"
    assert "system.compareVersions" in dist, "версії ніде не порівнюються"
    assert "CFBundleShortVersionString" in dist, "встановлена версія не читається"
    assert "__PKG_VERSION__" in dist, "версію пакета нікуди не підставляти"
    # Три випадки: старіша (пускаємо), та сама, новіша.
    assert "вже встановлена" in dist
    assert "новіша версія" in dist


def test_build_script_substitutes_the_package_version() -> None:
    """Номер версії в distribution підставляє збірка, а не людина руками."""
    script = "\n".join(_code_lines(REPO / "scripts" / "mac_build_pkg.sh"))

    assert "__PKG_VERSION__/$VERSION" in script, "підстановки версії немає"
    assert "DIST_RENDERED" in script and "--distribution \"$DIST_RENDERED\"" in script, (
        "productbuild збирає не з підставленого файла"
    )
    # І падаємо, якщо підстановка не спрацювала: інакше в інсталяторі
    # опиниться літерал `__PKG_VERSION__`.
    assert "версію в distribution не підставлено" in script
