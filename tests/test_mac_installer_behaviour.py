"""BH-155 — перевірки ПОВЕДІНКИ мак-інсталятора, а не наявності рядків.

Текстові перевірки в test_mac_installer_assets.py ловлять лише видалені
рядки. Ревʼю показало, чого вони не ловлять зовсім: переставлені місцями
гілки порівняння версій (тоді оновлення блокується, а відкат дозволяється),
рано вийдений цикл очікування, «успіх» при відповіді СТАРОЇ версії. Тут ці
випадки виконуються по-справжньому:

* postinstall — із підробленими `launchctl`, `pgrep`, `pkill` і `curl` у PATH,
  тобто видно, що робить сам скрипт, а не що в ньому написано;
* логіка порівняння версій — витягуємо JS із distribution і крутимо його в
  JavaScriptCore (`osascript -l JavaScript`) із заглушками Installer-API;
* збірка — робимо справжній пакет і читаємо, що в ньому лежить.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
POSTINSTALL = REPO / "installers" / "mac-postinstall.sh"
DIST = REPO / "installers" / "mac-distribution.xml"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="мак-інсталятор перевіряємо на macOS"
)


# ─── postinstall із підробленим launchd ──────────────────────────────────────


def _fake_bin(tmp: Path, *, bootstrap_ok: bool, health: str | None,
              still_running: bool = False) -> Path:
    """Тека з підробками launchctl/pgrep/pkill/curl.

    `health` — тіло, яке віддає /health (None = не відповідає нічого).
    """
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls = tmp / "calls.log"

    (bin_dir / "launchctl").write_text(
        "#!/bin/bash\n"
        f'echo "launchctl $*" >> "{calls}"\n'
        # asuser <uid> launchctl <cmd> … → дивимось на справжню підкоманду
        'args=("$@"); if [ "${args[0]}" = "asuser" ]; then args=("${args[@]:3}"); fi\n'
        'case "${args[0]}" in\n'
        '  bootstrap) exit ' + ("0" if bootstrap_ok else "1") + " ;;\n"
        '  print) exit 113 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (bin_dir / "pgrep").write_text(
        "#!/bin/bash\n"
        f'echo "pgrep $*" >> "{calls}"\n'
        f"exit {0 if still_running else 1}\n"
    )
    (bin_dir / "pkill").write_text(
        f'#!/bin/bash\necho "pkill $*" >> "{calls}"\nexit 0\n'
    )
    body = "" if health is None else health
    (bin_dir / "curl").write_text(
        "#!/bin/bash\n"
        f'echo "curl $*" >> "{calls}"\n'
        + ("exit 7\n" if health is None else f'printf %s {json.dumps(body)}\nexit 0\n')
    )
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir


def _run_postinstall(tmp: Path, bin_dir: Path, want_version: str = "9.9.9") -> subprocess.CompletedProcess[str]:
    """Прогнати postinstall на фальшивому томі з підставленою версією."""
    target = tmp / "vol"
    app = target / "Applications" / "Device Handler.app" / "Contents" / "MacOS"
    app.mkdir(parents=True, exist_ok=True)
    (app / "bhm").write_text("#!/bin/sh\n")
    (app / "bhm").chmod(0o755)

    # Скрипт беремо ТАКИМ, яким його кладе збірка: з підставленою версією.
    script = tmp / "postinstall"
    script.write_text(
        POSTINSTALL.read_text(encoding="utf-8").replace("__PKG_VERSION__", want_version)
    )
    script.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    # Домівка теж підроблена: скрипт пише туди plist і теку даних.
    env["HOME"] = str(tmp / "home")
    (tmp / "home").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(script), "pkg", "dest", "/"],
        capture_output=True, text=True, errors="replace", env=env, cwd=tmp,
    )


def test_reports_success_only_for_the_version_it_installed(tmp_path: Path) -> None:
    """Відповідь СТАРОЇ версії на 9999 — це не успіх.

    Знайдено ревʼю: перевірялось лише «хтось відповідає». Стара копія, яка ще
    не добила SIGTERM, теж відповідає — і оновлення, яке насправді не
    відбулося, звітувало «менеджер працює».
    """
    bin_dir = _fake_bin(tmp_path, bootstrap_ok=True,
                        health='{"status":"ok","version":"0.5.3"}')
    done = _run_postinstall(tmp_path, bin_dir, want_version="0.5.7")
    out = done.stdout + done.stderr

    assert "менеджер 0.5.7 працює" not in out, f"звітує успіх на чужу версію:\n{out}"
    assert "ІНША версія" in out, out


def test_reports_success_when_the_right_version_answers(tmp_path: Path) -> None:
    bin_dir = _fake_bin(tmp_path, bootstrap_ok=True,
                        health='{"status":"ok","version":"0.5.7"}')
    done = _run_postinstall(tmp_path, bin_dir, want_version="0.5.7")
    out = done.stdout + done.stderr

    assert "менеджер 0.5.7 працює" in out, out


def test_tells_the_truth_when_the_agent_never_starts(tmp_path: Path) -> None:
    """Не піднялось — так і кажемо, з командою, якою підняти вручну."""
    bin_dir = _fake_bin(tmp_path, bootstrap_ok=False, health=None)
    done = _run_postinstall(tmp_path, bin_dir)
    out = done.stdout + done.stderr

    assert "НЕ ВДАЛОСЯ підняти менеджер" in out, out
    assert "launchctl bootstrap" in out, "немає команди для ручного підняття"
    # Три спроби, а не одна.
    assert out.count("спроба") >= 3, out


def test_kills_the_old_process_that_refuses_to_die(tmp_path: Path) -> None:
    """Старий процес тримає порт 9999 — знімаємо силою.

    Знайдено ревʼю: без цього `bootstrap` звітує успіх, а новий екземпляр не
    може зайняти порт. install.sh це вже робить, у postinstall не робилось.
    """
    bin_dir = _fake_bin(tmp_path, bootstrap_ok=True, still_running=True,
                        health='{"status":"ok","version":"0.5.7"}')
    _run_postinstall(tmp_path, bin_dir, want_version="0.5.7")
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    assert "pkill -9" in calls, f"старий процес не знімається силою:\n{calls}"


def test_agent_is_bootstrapped_through_asuser(tmp_path: Path) -> None:
    """Підняття йде в сесії користувача, а не з root-контексту."""
    bin_dir = _fake_bin(tmp_path, bootstrap_ok=True,
                        health='{"status":"ok","version":"0.5.7"}')
    _run_postinstall(tmp_path, bin_dir, want_version="0.5.7")
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    assert re.search(r"asuser \d+ launchctl bootstrap", calls), calls
    # kickstart не кличемо навмисно: він перезапустив би СТАРУ реєстрацію.
    assert "kickstart" not in calls, calls


# ─── логіка порівняння версій із distribution ────────────────────────────────


def _installer_js() -> str:
    xml = DIST.read_text(encoding="utf-8")
    body = xml[xml.index("<![CDATA[") + len("<![CDATA[") : xml.index("]]>")]
    return body


def _decide(installed: str | None, pkg_version: str) -> dict:
    """Прогнати справжній JS інсталятора з заглушками Installer-API."""
    js = _installer_js().replace("__PKG_VERSION__", pkg_version)
    plist = (
        "null"
        if installed is None
        else json.dumps({"CFBundleShortVersionString": installed})
    )
    harness = f"""
var my = {{ result: {{}} }};
var system = {{
  files: {{
    fileExistsAtPath: function (p) {{ return {json.dumps(installed is not None)}; }},
    plistAtPath: function (p) {{ return {plist}; }}
  }},
  compareVersions: function (a, b) {{
    var pa = String(a).split('.'), pb = String(b).split('.');
    for (var i = 0; i < Math.max(pa.length, pb.length); i++) {{
      var x = parseInt(pa[i] || '0', 10), y = parseInt(pb[i] || '0', 10);
      if (x > y) return 1;
      if (x < y) return -1;
    }}
    return 0;
  }}
}};
{js}
var allowed = check_before_install();
JSON.stringify({{ allowed: allowed, title: my.result.title || '', type: my.result.type || '' }});
"""
    done = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", harness],
        capture_output=True, text=True, errors="replace",
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip())


def test_older_install_is_upgraded_without_a_warning() -> None:
    """Той випадок, по який людина й прийшла.

    Слова власника: «повинен би версії перевіряти і пропонувати оновлення, а
    не казати шо вже встановлено».
    """
    r = _decide(installed="0.5.3", pkg_version="0.5.7")
    assert r["allowed"] is True, r
    assert r["title"] == "", f"оновлення не має ні про що попереджати: {r}"


def test_same_version_offers_a_reinstall() -> None:
    r = _decide(installed="0.5.7", pkg_version="0.5.7")
    assert r["allowed"] is False
    assert "вже встановлена" in r["title"], r
    assert r["type"] == "Warn", "Fatal замкнув би людину без можливості полікувати"


def test_newer_install_warns_about_a_downgrade() -> None:
    r = _decide(installed="0.6.0", pkg_version="0.5.7")
    assert r["allowed"] is False
    assert "новіша версія" in r["title"], r
    assert r["type"] == "Warn"


def test_version_numbers_compare_as_numbers_not_text() -> None:
    """0.5.10 новіша за 0.5.9, хоч текстом менша."""
    r = _decide(installed="0.5.9", pkg_version="0.5.10")
    assert r["allowed"] is True, f"оновлення 0.5.9 → 0.5.10 заблоковано: {r}"

    r = _decide(installed="0.5.10", pkg_version="0.5.9")
    assert r["allowed"] is False and "новіша" in r["title"], r


def test_clean_machine_installs_without_questions() -> None:
    r = _decide(installed=None, pkg_version="0.5.7")
    assert r["allowed"] is True and r["title"] == "", r


# ─── збірка ──────────────────────────────────────────────────────────────────


def _fixture_app(root: Path, version: str) -> Path:
    app = root / "Applications" / "Device Handler.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "bhm").write_text("#!/bin/sh\n")
    (app / "Contents" / "MacOS" / "bhm").chmod(0o755)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({
            "CFBundleIdentifier": "com.goodpesik.barhandler-manager",
            "CFBundleName": "Device Handler",
            "CFBundleExecutable": "bhm",
            "CFBundleShortVersionString": version,
            "CFBundlePackageType": "APPL",
        })
    )
    return app


@pytest.mark.skipif(shutil.which("pkgbuild") is None, reason="немає pkgbuild")
def test_built_package_carries_the_version_in_both_places(tmp_path: Path) -> None:
    """Версія мусить доїхати і в distribution, і в postinstall.

    Текстова перевірка sed-рядка нічого не доводить: важливо, що лежить у
    ЗІБРАНОМУ пакеті (знайдено ревʼю).
    """
    app = _fixture_app(tmp_path / "root", "1.2.3")
    work = tmp_path / "build"
    work.mkdir()
    env = dict(os.environ)
    (work / "VERSION").write_text("1.2.3\n")

    done = subprocess.run(
        ["bash", str(REPO / "scripts" / "mac_build_pkg.sh"), str(app), str(work / "out.pkg")],
        capture_output=True, text=True, errors="replace", cwd=work,
        env={**env, "MAC_PKG_DISTRIBUTION": str(DIST),
             "MAC_PKG_RESOURCES": str(REPO / "installers" / "mac-resources"),
             "MAC_PKG_POSTINSTALL": str(POSTINSTALL)},
    )
    assert done.returncode == 0, done.stdout + done.stderr

    exp = tmp_path / "exp"
    subprocess.run(["pkgutil", "--expand", str(work / "out.pkg"), str(exp)], check=True)
    assert 'PKG_VERSION = "1.2.3"' in (exp / "Distribution").read_text(encoding="utf-8")

    # `pkgutil --expand` віддає Scripts текою, не архівом.
    postinstall = next(exp.glob("*.pkg/Scripts/postinstall"))
    assert 'WANT_VERSION="1.2.3"' in postinstall.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("pkgbuild") is None, reason="немає pkgbuild")
def test_empty_version_file_fails_the_build(tmp_path: Path) -> None:
    """Порожній VERSION — зламана збірка, а не «0.0.0».

    Знайдено ревʼю: `|| echo 0.0.0` ловить лише невдале ЧИТАННЯ файла, тож
    порожній файл давав `PKG_VERSION = ""`, і інсталятор порівнював версію з
    порожнім рядком.
    """
    app = _fixture_app(tmp_path / "root", "1.2.3")
    work = tmp_path / "build"
    work.mkdir()
    (work / "VERSION").write_text("\n")

    done = subprocess.run(
        ["bash", str(REPO / "scripts" / "mac_build_pkg.sh"), str(app), str(work / "out.pkg")],
        capture_output=True, text=True, errors="replace", cwd=work,
        env={**dict(os.environ), "MAC_PKG_DISTRIBUTION": str(DIST),
             "MAC_PKG_RESOURCES": str(REPO / "installers" / "mac-resources"),
             "MAC_PKG_POSTINSTALL": str(POSTINSTALL)},
    )
    assert done.returncode != 0, "збірка з порожнім VERSION мусить падати"
    assert "VERSION" in done.stdout + done.stderr


# ─── той самий клас вади в install.sh ────────────────────────────────────────
#
# BH-155 виправив реєстрацію агента в mac-postinstall.sh. Але клас вади —
# «служба реєструється в домен root замість домену користувача» — жив і в
# install.sh: `gui/$(id -u)` під sudo це `gui/0`, а plist ляже в
# /var/root/Library/LaunchAgents. Скрипт при цьому звітує успіх.
#
# Правило, яке я порушував тричі за сесію: полікувавши екземпляр, перелічити
# ВСІ місця того самого класу.


def _run_install_as_fake_root(tmp_path: Path, sudo_user: str | None) -> subprocess.CompletedProcess[str]:
    """Прогнати install.sh із підробленим `id -u` = 0 (справжній root не треба)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "id").write_text('#!/bin/bash\nif [ "$1" = "-u" ]; then echo 0; else /usr/bin/id "$@"; fi\n')
    (bin_dir / "uname").write_text('#!/bin/bash\nif [ "$1" = "-s" ]; then echo Darwin; else /usr/bin/uname "$@"; fi\n')
    # sudo не викликаємо по-справжньому: якщо скрипт спробує, це видно в логу.
    calls = tmp_path / "sudo.log"
    (bin_dir / "sudo").write_text(f'#!/bin/bash\necho "sudo $*" >> "{calls}"\nexit 0\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir(exist_ok=True)
    if sudo_user:
        env["SUDO_USER"] = sudo_user
    else:
        env.pop("SUDO_USER", None)
    return subprocess.run(
        ["bash", str(REPO / "installers" / "install.sh")],
        capture_output=True, text=True, errors="replace", env=env, cwd=tmp_path,
    )


def test_install_sh_refuses_to_run_as_plain_root(tmp_path: Path) -> None:
    """Без SUDO_USER незрозуміло, кому належатиме агент — відмовляємось."""
    done = _run_install_as_fake_root(tmp_path, sudo_user=None)
    out = done.stdout + done.stderr

    assert done.returncode != 0, f"скрипт пішов далі від root:\n{out}"
    assert "без sudo" in out, out


def test_install_sh_reexecs_as_the_invoking_user(tmp_path: Path) -> None:
    """Під sudo — перезапуск від того, хто його викликав, а не установка root-ові."""
    done = _run_install_as_fake_root(tmp_path, sudo_user="maksym")
    out = done.stdout + done.stderr
    log = (tmp_path / "sudo.log").read_text(encoding="utf-8") if (tmp_path / "sudo.log").exists() else ""

    assert "-u maksym" in log, f"перезапуску від користувача не було:\n{out}\n{log}"


def _decide_paths(existing: dict[str, str | None], pkg_version: str) -> dict:
    """Як `_decide`, але з РІЗНИМИ шляхами: ключ — шлях, значення — версія.

    Значення None = бандл є, а версію прочитати не вдалося. Потрібно, щоб
    перевірити гілку для старої назви бандла й гілку «версія невідома» —
    обидві лишались без жодного тесту (знайдено другим колом ревʼю).
    """
    js = _installer_js().replace("__PKG_VERSION__", pkg_version)
    plists = {
        path: (None if ver is None else {"CFBundleShortVersionString": ver})
        for path, ver in existing.items()
    }
    harness = f"""
var PLISTS = {json.dumps(plists)};
var my = {{ result: {{}} }};
var system = {{
  files: {{
    fileExistsAtPath: function (p) {{ return PLISTS.hasOwnProperty(p); }},
    plistAtPath: function (p) {{ return PLISTS.hasOwnProperty(p) ? PLISTS[p] : null; }}
  }},
  compareVersions: function (a, b) {{
    var pa = String(a).split('.'), pb = String(b).split('.');
    for (var i = 0; i < Math.max(pa.length, pb.length); i++) {{
      var x = parseInt(pa[i] || '0', 10), y = parseInt(pb[i] || '0', 10);
      if (x > y) return 1;
      if (x < y) return -1;
    }}
    return 0;
  }}
}};
{js}
var allowed = check_before_install();
JSON.stringify({{ allowed: allowed, title: my.result.title || '', message: my.result.message || '' }});
"""
    done = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", harness],
        capture_output=True, text=True, errors="replace",
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip())


NEW_PLIST = "/Applications/Device Handler.app/Contents/Info.plist"
OLD_PLIST = "/Applications/BarhandlerManager.app/Contents/Info.plist"


def test_version_is_read_from_the_pre_BH150_bundle_too() -> None:
    """Стара назва бандла теж має читатись.

    У полі ще є установки до BH-150 (`BarhandlerManager.app`). Перші тести
    цього не перевіряли: заглушка віддавала одну й ту саму відповідь на будь-
    який шлях, тож гілка зі старою назвою не виконувалась ніколи.
    """
    r = _decide_paths({OLD_PLIST: "0.5.3"}, "0.5.7")
    assert r["allowed"] is True, f"оновлення поверх старого бандла заблоковано: {r}"

    r = _decide_paths({OLD_PLIST: "0.5.7"}, "0.5.7")
    assert r["allowed"] is False and "вже встановлена" in r["title"], r


def test_new_bundle_wins_over_the_legacy_one() -> None:
    """Коли є обидва — дивимось на НОВИЙ, він і працює."""
    r = _decide_paths({NEW_PLIST: "0.5.7", OLD_PLIST: "0.1.0"}, "0.5.7")
    assert r["allowed"] is False and "вже встановлена" in r["title"], r


def test_unreadable_version_says_so_instead_of_guessing() -> None:
    """Бандл є, версії не прочитали — окрема гілка, яку ніхто не перевіряв."""
    r = _decide_paths({NEW_PLIST: None}, "0.5.7")
    assert r["allowed"] is False
    assert "прочитати не вдалося" in r["message"], r
    assert "0.5.7" in r["message"], "не сказано, на що саме замінимо"
