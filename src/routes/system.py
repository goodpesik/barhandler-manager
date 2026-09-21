"""System management endpoints — update + version info + uplink toggle.

POST /system/update  — spawns update.sh as a detached process and returns
                       immediately. The manager will restart itself within
                       a few seconds as the installer replaces the code.
GET  /system/version — returns the running version so the dashboard JS
                       can compare it with the latest GitHub release without
                       needing a dedicated GitHub API proxy.
GET  /system/uplink  — current uplink config + live connection status.
POST /system/uplink  — write a new uplink block into config.yaml and
                       SIGTERM self so the service manager respawns with
                       the new state.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()

IS_WIN = os.name == "nt"
# A frozen build is the standalone Windows .exe (PyInstaller). It has no
# .barhandler-manager checkout and no update.sh/.ps1 — it updates by
# downloading the latest installer and running it (see _build_update_argv).
FROZEN = bool(getattr(sys, "frozen", False))

from src.config import APP_DIR
from src.version import installed_version
from src.services.busy import busy_refusal

# Встановлений мак-застосунок (.dmg/.pkg), а не скриптова інсталяція й не вінда.
IS_MAC_APP_INSTALL = bool(getattr(sys, "frozen", False)) and sys.platform == "darwin"

_INSTALL_DIR = Path.home() / ".barhandler-manager"
# The exe has no ~/.barhandler-manager; keep its update.log next to the exe
# (APP_DIR), which the installer leaves in place across upgrades.
_UPDATE_LOG = (APP_DIR if FROZEN else _INSTALL_DIR) / "update.log"
# Скільки чекати, перш ніж питати, чи дитина ще жива. Команда оновлення
# починається з двосекундної паузи, тож цього досить, щоб відрізнити
# «стартувала» від «померла одразу», і замало, щоб людина помітила.
_DEAD_CHILD_GRACE_SECONDS = 0.4

# Прапорці запуску вінди — ЧИСЛАМИ: у `subprocess` їх немає на posix, тож
# навіть згадка `subprocess.CREATE_NO_WINDOW` падає на маку (і в тестах теж).
WIN_CREATE_NO_WINDOW = 0x08000000
WIN_CREATE_NEW_PROCESS_GROUP = 0x00000200
WIN_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
WIN_DETACHED_PROCESS = 0x00000008


# BH-176 — на вінді оновлення качає БРАУЗЕР, а не ми.
#
# Ми пробували зробити це самі: завантажити інсталятор і відкрити його з
# нашого процесу. Цей шлях щоразу ламався по-різному — то беззвучний режим і
# невидимий діалог SmartScreen (BH-161), то дитина, що вмирала разом із нами
# (BH-174), то новий процес не міг зайняти порт, бо старий ще тримав його, і
# дашборд далі показував стару версію.
#
# Той самий інсталятор, завантажений людиною з браузера й запущений руками,
# ставиться без жодної з цих пригод. Тож кнопка «Оновити» тепер просто віддає
# браузеру адресу файла: далі все робить сама людина, як вона й робила б.
WIN_INSTALLER_URL = (
    "https://github.com/goodpesik/barhandler-manager"
    "/releases/latest/download/device-handler-setup.exe"
)


@router.get("/version")
async def get_version() -> dict:
    version = installed_version("unknown")
    # BH-150 — дашборд має знати, чи це встановлений мак-застосунок: кнопку
    # видалення показуємо ЛИШЕ там, де вона справді щось знімає. У скриптовій
    # інсталяції та на вінді за це відповідають їхні власні інсталятори.
    return {
        "version": version,
        "mac_app_install": IS_MAC_APP_INSTALL,
        # Порожньо — оновлення робимо самі (мак, скриптова інсталяція).
        "update_download_url": WIN_INSTALLER_URL if (IS_WIN and FROZEN) else None,
    }


def _mac_host_arch() -> str:
    """«silicon» або «intel» — архітектура МАШИНИ, не процесу.

    Знайдено ревʼю: `platform.machine()` віддає архітектуру процесу. Intel-збірка
    під Rosetta на Apple Silicon репортує x86_64 — і кнопка «Оновити» назавжди
    підсовувала б Intel-пакет, тобто машина лишалась би на трансляції довіку й
    сама б із цього не вибралась (а таке буває: перенесли користувача з
    Intel-мака через Migration Assistant або поставили не той пакет).

    `sysctl.proc_translated` = 1 означає «цей процес іде під Rosetta», а отже
    сам хост — arm64. Ключ існує лише на маку й лише під трансляцією, тож
    відсутність або помилка = не транслюємось.
    """
    try:
        translated = subprocess.run(
            ["sysctl", "-n", "sysctl.proc_translated"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        translated = ""
    if translated == "1":
        return "silicon"
    return "silicon" if platform.machine() == "arm64" else "intel"


def _win_script_path() -> Path:
    """Куди кладемо тіло оновлення. У TEMP, а не поруч із exe: там пише будь-хто
    без прав адміністратора.

    Імʼя унікальне на кожен запуск: кнопку тиснуть по кілька разів підряд (у
    полі так і було — три спроби за хвилину), і спільний файл означав би, що
    новий запис лягає під ноги тому PowerShell, який ще його читає.
    """
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return Path(tempfile.gettempdir()) / f"device-handler-update-{stamp}.ps1"


def _win_launcher(body: str) -> str:
    """BH-174 — як саме запускати PowerShell на вінді, щоб він дожив і лишив слід.

    Доти ми передавали все тіло через `-Command` і чекали, що вивід сам потрапить
    у `update.log` через успадкований дескриптор. У полі не потрапляло НІЧОГО:
    у лозі стояли самі наші заголовки, без жодного рядка PowerShell — навіть без
    першого `Write-Host`, тобто процес помирав раніше, ніж щось написав. Причина
    в парі «`DETACHED_PROCESS` + консольний хост»: дитина лишається зовсім без
    консолі, а `powershell.exe` без неї просто гине, і подивитись на це нікому —
    на процес ніхто не чекає.

    Тому: тіло пишемо у файл, а запускаємо його через `cmd`, і перенаправлення
    в лог робить САМ `cmd` (`>> "лог" 2>&1`). Тоді вивід лягає в лог незалежно
    від того, що там успадкувалось, а вікно ховає `CREATE_NO_WINDOW` (див.
    `trigger_update`) — замість `DETACHED_PROCESS`, який консоль і забирав.
    """
    script = _win_script_path()
    script.write_text(body, encoding="utf-8")
    inner = (
        f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}"'
        f' >> "{_UPDATE_LOG}" 2>&1'
    )
    # РЯДКОМ, а не списком. Список Python серіалізує через `list2cmdline`, і та
    # екранує внутрішні лапки бекслешем (`\"`) — за конвенцією C-рантайму. Але
    # `cmd.exe` бекслеш як екранування НЕ розуміє: він побачив би шлях
    # `\C:\…\update.ps1\` і не відкрив би ні скрипта, ні лога. Саме так фікс
    # і зламався б непоміченим (знайшло ревʼю).
    #
    # Зовнішня пара лапок потрібна: `cmd /c` за своїм правилом знімає ПЕРШУ й
    # ОСТАННЮ лапку рядка, і далі команда читається з нормальними парними
    # лапками навколо шляхів — тобто пробіли в «Program Files» переживають.
    return f'cmd.exe /c "{inner}"' 


def _build_update_argv() -> tuple[list[str], str]:
    """Return the (argv, human-readable description) for the update
    command on this OS. POSIX runs update.sh via bash; Windows runs
    update.ps1 via powershell. Each falls back to pulling the upstream
    installer directly when the on-disk helper is missing (e.g. a dev
    checkout that was never `install`-ed).

    The 2-second sleep gives the HTTP response time to reach the browser
    before the manager restarts out from under it.
    """
    if IS_WIN and FROZEN:
        # Standalone .exe: завантажити інсталятор і ВІДКРИТИ його — так само,
        # як це робить мак-збірка зі своїм .pkg.
        #
        # BH-161. Доти ми запускали його з `/VERYSILENT /SUPPRESSMSGBOXES`, і
        # саме тиша все й зіпсувала. Наш інсталятор не підписаний, а файл,
        # завантажений через `Invoke-WebRequest`, несе мітку Mark-of-the-Web —
        # SmartScreen таку пару блокує діалогом. У беззвучному режимі той
        # діалог невидимий: `-Wait` висить, у лозі ані рядка, менеджер живий зі
        # старим pid. Рівно те, що ми бачили в клієнта, і розібрати це було
        # нічим. Репутація SmartScreen рахується за хешем файлу, тож черговий
        # реліз може почати блокуватись, хоч ми в цьому шляху нічого не міняли.
        #
        # Відкритий інсталятор робить помилку видимою людині: майстер показує
        # і попередження SmartScreen, і запит прав. Вона натискає — і воно йде.
        # Мінус у тому, що оновлення перестало бути автоматичним; плюс — воно
        # перестало мовчки не відбуватись.
        #
        # Ім'я активу нове (BH-148/BH-149), і саме тому реліз публікує ще й
        # КОПІЮ під старим ім'ям barhandler-setup.exe: копії, вже встановлені
        # в полі, тягнуть старе ім'я цим самим кодом зі СВОЄЇ версії. Прибрати
        # старий актив можна буде тоді, коли таких інсталяцій не лишиться.
        inner = (
            "$ErrorActionPreference = 'Stop'; "
            "Start-Sleep -Seconds 2; "
            "$u = 'https://github.com/goodpesik/barhandler-manager"
            "/releases/latest/download/device-handler-setup.exe'; "
            "$tmp = Join-Path $env:TEMP 'device-handler-setup.exe'; "
            "try { "
            "Write-Host \"update: downloading $u\"; "
            "Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri $u -OutFile $tmp; "
            "$len = (Get-Item $tmp).Length; "
            "Write-Host \"update: downloaded $len bytes to $tmp\"; "
            "if ($len -lt 100000) { "
            "Write-Host 'update: installer download too small - nothing changed'; exit 1 }; "
            # Мітку знімаємо все одно: менше причин для зайвого попередження.
            "Unblock-File -Path $tmp -ErrorAction SilentlyContinue; "
            "Write-Host 'update: opening installer for the operator'; "
            # БЕЗ -Wait і без беззвучних прапорців: майстер відкривається перед
            # людиною, а ця команда на нього не чекає — інакше вона висіла б
            # рівно стільки, скільки людина його не бачить.
            "Start-Process -FilePath $tmp; "
            "Write-Host 'update: installer opened' "
            "} catch { "
            "Write-Host \"update: FAILED - $($_.Exception.Message)\"; exit 1 }"
        )
        return _win_launcher(inner), inner

    if IS_WIN:
        script = _INSTALL_DIR / "update.ps1"
        if script.exists():
            inner = f"Start-Sleep -Seconds 2; & '{script}'"
        else:
            # Fallback: pull install.ps1 and run it in upgrade mode.
            # Invoke-WebRequest throws on a failed download (unlike a
            # silent `curl|bash`), so a network blip surfaces in the log.
            inner = (
                "Start-Sleep -Seconds 2; "
                "$r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri "
                "'https://github.com/goodpesik/barhandler-manager"
                "/releases/latest/download/install.ps1'; "
                "if (-not $r.Content -or $r.Content.Length -lt 100) { "
                "Write-Host 'update: empty installer downloaded — nothing changed'; exit 1 }; "
                'Invoke-Expression "& { $($r.Content) } -Force"'
            )
        return _win_launcher(inner), inner

    if IS_MAC_APP_INSTALL:
        # BH-150 — мак-застосунок оновлюється СВОЇМ інсталятором, а не
        # скриптом. Знайдено ревʼю: доти ця гілка провалювалась у POSIX-шлях
        # нижче, тобто `curl | bash install.sh`, і та команда ставила поруч
        # СКРИПТОВУ інсталяцію — інший спосіб установки, який ще й воює за
        # порт 9999. Тобто кнопка «Оновити» в мак-застосунку не оновлювала
        # його ніколи.
        #
        # `open` віддає пакет системному інсталятору: там і прогрес, і
        # запит прав адміністратора, який агент користувача сам дати не
        # може. Архітектуру беремо з МАШИНИ — див. _mac_host_arch().
        arch = _mac_host_arch()
        asset = f"device-handler-{arch}.pkg"
        cmd = (
            "sleep 2 && set -o pipefail && "
            'TMP="$(mktemp -d "${TMPDIR:-/tmp}/bhm-pkg.XXXXXX")" && '
            f'curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/{asset} '
            f'-o "$TMP/{asset}" && '
            f'{{ [ -s "$TMP/{asset}" ] || {{ echo "✗ update: порожній пакет — нічого не змінено" >&2; exit 1; }}; }} && '
            f'open "$TMP/{asset}"'
        )
        return ["bash", "-c", cmd], cmd

    script = _INSTALL_DIR / "update.sh"
    if not script.exists():
        # Fallback: inline the update command directly. Download to a
        # temp file and verify it's non-empty BEFORE running it — a
        # piped `curl | bash` turns a transient GitHub failure into an
        # empty script that bash runs as a silent no-op (exit 0, nothing
        # changed). Here a failed download exits non-zero and lands in
        # update.log instead of pretending success.
        cmd = (
            "sleep 2 && set -o pipefail && "
            'TMP="$(mktemp "${TMPDIR:-/tmp}/bhm-install.XXXXXX")" && '
            "curl -fsSL https://github.com/goodpesik/barhandler-manager"
            '/releases/latest/download/install.sh -o "$TMP" && '
            '{ [ -s "$TMP" ] || { echo "✗ update: empty installer downloaded — nothing changed" >&2; rm -f "$TMP"; exit 1; }; } && '
            'bash "$TMP" --force; rc=$?; rm -f "$TMP"; exit $rc'
        )
    else:
        cmd = f"sleep 2 && bash {script}"
    return ["bash", "-c", cmd], cmd


def _update_is_interactive() -> bool:
    """Чи оновлення чекає на ЛЮДИНУ, а не ставиться саме.

    Там, де ми ВІДКРИВАЄМО інсталятор (мак-застосунок із BH-150, вінда-збірка
    з BH-161), нічого не станеться доти, доки людина не пройде майстер і не
    дасть права. Скриптова інсталяція справді ставить усе сама.

    Це ознака для ТИХ, ХТО ВИКЛИКАЄ endpoint, а не текст для читання: і каса,
    і дашборд мусять вести себе по-різному в цих двох випадках, і розбирати
    для цього український рядок — те саме, що не мати ознаки взагалі.
    """
    return IS_MAC_APP_INSTALL or (IS_WIN and FROZEN)


def _update_started_message() -> str:
    """Що сказати людині, яка щойно натиснула «Оновити».

    Обіцяти «перезапуститься за ~30 секунд» там, де відкривається майстер, —
    неправда, і саме через таку неправду людина йде, а оновлення не
    відбувається.
    """
    if _update_is_interactive():
        return "Відкрився інсталятор — пройдіть його, і менеджер оновиться"
    return "Оновлення запущено — менеджер перезапуститься за ~30 секунд"


@router.post("/update")
async def trigger_update(request: Request) -> dict:
    """Spawn the platform updater fully detached from the manager so it
    survives the restart it triggers, and return immediately.

    Відмовляє з 409, поки менеджер посеред незворотної роботи — див.
    `busy_refusal`. Ця перевірка НЕ єдина: інсталятори питають те саме через
    `GET /busy` безпосередньо перед тим, як убивати процес, бо між натисканням
    кнопки й реальним `taskkill` може пройти скільки завгодно часу.

    Detachment differs per OS:
      * POSIX  — `start_new_session=True` (own session, survives the
        SIGTERM the installer sends the manager).
      * Windows — DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP, plus
        CREATE_BREAKAWAY_FROM_JOB so the updater leaves the Scheduled
        Task's job object. Without breakaway, stopping the manager task
        tree-kills this updater mid-flight. Some job configs forbid
        breakaway (CreateProcess fails); we retry without it in that case.

    stdout+stderr go to update.log (NOT DEVNULL) so a silent failure —
    GitHub unreachable, launchctl/systemd refusing the reload, a missing
    dep — leaves the operator something to read instead of a frozen
    "Перезапуск…" button. Append-mode preserves earlier attempts.
    """
    busy = busy_refusal(request)
    if busy is not None:
        raise HTTPException(status_code=409, detail=busy)

    # BH-176 — на вінді ми інсталятор НЕ запускаємо: браузер качає файл, людина
    # запускає його сама (див. WIN_INSTALLER_URL). Дашборд сюди вже не
    # звертається, але стара відкрита вкладка може — тоді відповідаємо
    # адресою, а не мовчазною спробою, яка однаково не спрацює.
    if IS_WIN and FROZEN:
        return {
            "started": False,
            "interactive": True,
            "download_url": WIN_INSTALLER_URL,
            "message": "Завантажте інсталятор і запустіть його — менеджер оновиться",
        }

    argv, desc = _build_update_argv()

    try:
        _INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        # Append header so the operator can tell separate attempts apart
        # in update.log when they bang the button multiple times.
        with _UPDATE_LOG.open("a") as fh:
            fh.write(
                f"\n=== update triggered {_dt.datetime.now().isoformat()} "
                f"(pid={os.getpid()}, version={installed_version()}) ===\n",
            )
            fh.write(f"cmd: {desc}\n")
            fh.flush()

        popen_kwargs: dict = dict(
            stdout=None,  # POSIX: нижче стає дескриптором лога
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
        if IS_WIN:
            # BH-174 — БЕЗ `DETACHED_PROCESS`: він лишає дитину зовсім без
            # консолі, а `powershell.exe` без неї гине мовчки, не написавши
            # жодного рядка. `CREATE_NO_WINDOW` так само не показує вікна, але
            # консоль у процесу є.
            popen_kwargs["creationflags"] = (
                WIN_CREATE_NO_WINDOW
                | WIN_CREATE_NEW_PROCESS_GROUP
                | WIN_CREATE_BREAKAWAY_FROM_JOB
            )
            # Вивід у лог пише сам `cmd` (`>>` у команді): через успадкований
            # дескриптор він до лога не доходив.
            popen_kwargs["stdout"] = subprocess.DEVNULL
            popen_kwargs["stderr"] = subprocess.DEVNULL
        else:
            popen_kwargs["start_new_session"] = True
            # When the manager runs under launchd / systemd the inherited
            # PATH is the bare service-context one and doesn't include
            # Homebrew. install.sh prepends those prefixes itself now, but
            # set a sane PATH here too so anything that runs before
            # install.sh sources its own PATH still resolves brew/python3.
            popen_kwargs["env"] = {
                **os.environ,
                "PATH": (
                    "/opt/homebrew/bin:/opt/homebrew/sbin:"
                    "/usr/local/bin:/usr/local/sbin:"
                    + os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
                ),
            }

        # BH-174 — на вінді дескриптор лога дитині НЕ віддаємо (вище стоїть
        # DEVNULL, а пише в лог сам `cmd`); цей рядок доти перекривав його
        # назад, скасовуючи те, що зробили трьома рядками вище.
        log_fh = _UPDATE_LOG.open("a")
        if not IS_WIN:
            popen_kwargs["stdout"] = log_fh
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(argv, **popen_kwargs)
        except OSError:
            # Breakaway can be refused by the job object — retry without
            # it rather than failing the whole update.
            if IS_WIN:
                popen_kwargs["creationflags"] = (
                    WIN_CREATE_NO_WINDOW | WIN_CREATE_NEW_PROCESS_GROUP
                )
                proc = subprocess.Popen(argv, **popen_kwargs)
            else:
                raise
        finally:
            # Popen dup'd the fd; close our handle so it doesn't leak.
            log_fh.close()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"не вдалось запустити оновлення: {exc}") from exc

    # BH-174 — «запущено» не має бути здогадом. Дитина, що вмирає одразу
    # (немає `powershell`, політика, відмова job-обʼєкта), виглядала точно так
    # само, як успішний старт: `Popen` не падає, а на процес ніхто не дивиться.
    # Тож даємо їй мить і питаємо код повернення: команда починається з
    # двосекундної паузи, тож жива дитина тут ще працює.
    await asyncio.sleep(_DEAD_CHILD_GRACE_SECONDS)
    rc = proc.poll() if proc is not None else None
    if rc is not None:
        try:
            with _UPDATE_LOG.open("a") as fh:
                fh.write(f"update: launcher exited immediately rc={rc}\n")
        except OSError:
            pass
        raise HTTPException(
            status_code=500,
            detail=f"оновлення не запустилось (код {rc}) — подивіться {_UPDATE_LOG}",
        )

    message = _update_started_message()
    return {
        "status": "updating",
        "message": message,
        # `interactive: true` = відкрився майстер, і поки людина його не
        # пройде, версія не зміниться. Каса й дашборд читають саме це поле:
        # інакше кожен із них мусив би вгадувати стан за текстом повідомлення.
        "interactive": _update_is_interactive(),
        "log": str(_UPDATE_LOG),
    }


# BH-150 — видалення мак-збірки.
#
# Власник просив, щоб установка поводилась як установка: інсталятор на
# повторному запуску каже «вже встановлено», а зняти менеджер можна кнопкою —
# не через термінал і не перетягуванням у корзину, після якого лишаються
# агент автозапуску й тека даних.
_MAC_APP = Path("/Applications/Device Handler.app")
# Назва бандла до BH-150. Видалення мусить знати обидві: у полі є інсталяції,
# поставлені під старою назвою, і лишити їх означає лишити робочий агент, який
# «повернe» менеджер при наступному вході.
_MAC_APP_LEGACY = Path("/Applications/BarhandlerManager.app")
_MAC_AGENT_LABEL = "com.goodpesik.barhandler-manager"
_MAC_AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{_MAC_AGENT_LABEL}.plist"
_UNINSTALL_LOG = APP_DIR / "uninstall.log"

def _build_uninstall_script(purge_data: bool) -> str:
    """Команда видалення. Порядок кроків тут — не косметика.

    Спершу знімаємо агент і лише потім зносимо застосунок: `KeepAlive: true`
    підняв би його заново, якби файл зник раніше за plist, і ми отримали б
    процес без застосунку, який launchd безкінечно перезапускає.

    Теку даних зносимо ЛИШЕ на явну згоду: там конфіг і зареєстровані
    принтери, тобто робота, яку людина робила руками.
    """
    steps = [
        "sleep 2",
        f'launchctl bootout "gui/$(id -u)/{_MAC_AGENT_LABEL}" 2>/dev/null || true',
        f'rm -f "{_MAC_AGENT_PLIST}"',
        # Агент міг бути покладений трьома способами, і знімати треба всі:
        #   • у домівці — старий шлях і .dmg-установка;
        #   • у /Library/LaunchAgents — headless-установка пакетом (там нікого
        #     не було залогінено, тож агент поклали для всіх);
        #   • через SMAppService — агент із бандла; він зникає разом із
        #     застосунком, але system-плист треба прибрати руками.
        # rm у /Library потребує прав, яких у агента немає — тому `|| true`:
        # не змогли, то й не змогли, решта видалення має доробитись.
        f'rm -f "/Library/LaunchAgents/{_MAC_AGENT_LABEL}.plist" 2>/dev/null || true',
        f'rm -rf "{_MAC_APP}"',
        f'rm -rf "{_MAC_APP_LEGACY}"',
    ]
    if purge_data:
        steps.append(f'rm -rf "{APP_DIR}"')
    # Себе вбиваємо останнім: доти скрипт має доробити все інше. -f саме по
    # шляху бінарника в бандлі — щоб не влучити в скриптову інсталяцію, якщо
    # людина тримає обидві.
    # Обидві назви бандла: нова й та, під якою стоять інсталяції до BH-150.
    steps.append(
        'pkill -f "Device Handler.app/Contents/MacOS/bhm" 2>/dev/null || true; '
        'pkill -f "BarhandlerManager.app/Contents/MacOS/bhm" 2>/dev/null || true',
    )
    return " && ".join(steps[:-1]) + "; " + steps[-1]


@router.post("/uninstall")
async def trigger_uninstall(request: Request, purge_data: bool = False) -> dict:
    """Знести мак-збірку: агент автозапуску, застосунок і (за згодою) дані.

    Тільки для встановленої мак-збірки. Скриптову інсталяцію знімає її власний
    stop.sh/uninstall у ~/.barhandler-manager, а на вінді це робить Inno, тож
    підміняти їх звідси — шлях до половинчасто знесених інсталяцій.
    """
    if not IS_MAC_APP_INSTALL:
        raise HTTPException(
            status_code=400,
            detail="кнопка видалення працює лише для застосунку macOS з Applications",
        )

    # Знайдено ревʼю. Ключ API у нас статичний і лежить у відкритому репо, а
    # cors_origin_regex пускає будь-який сайт на *.web.app — Firebase Hosting
    # безкоштовний, тож «будь-який» тут буквальне. Досі найгірше, що можна було
    # зробити таким запитом, — надрукувати чек; тепер тут незворотне видалення,
    # тому окрема умова: якщо запит прийшов із чужої сторінки, відмовляємо.
    # Дашборд ходить із localhost або взагалі без Origin (curl), інструменти
    # діагностики — так само.
    origin = request.headers.get("origin")
    if origin and not re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", origin):
        raise HTTPException(
            status_code=403,
            detail="видалення можна запустити лише зі сторінки менеджера",
        )

    # BH-164, знайдено другим колом ревʼю: видалення робить той самий `pkill`,
    # що й оновлення, і так само незворотне — а перевірки зайнятості тут не було
    # взагалі. Кнопка видалення живе в дашборді й доступна посеред зміни: зняти
    # менеджер посеред оплати карткою означає списану картку, про яку каса не
    # дізнається ніколи. Ціна помилки тут навіть вища за оновлення, бо після
    # видалення менеджер не повернеться сам.
    busy = busy_refusal(request)
    if busy is not None:
        raise HTTPException(status_code=409, detail=busy)

    cmd = _build_uninstall_script(purge_data)
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with _UNINSTALL_LOG.open("a") as fh:
            fh.write(
                f"\n=== uninstall triggered {_dt.datetime.now().isoformat()} "
                f"(pid={os.getpid()}, purge_data={purge_data}) ===\n",
            )
            fh.write(f"cmd: {cmd}\n")
            fh.flush()
        log_fh = _UNINSTALL_LOG.open("a")
        try:
            # start_new_session — інакше скрипт помре разом із процесом, який
            # він же й убиває. Той самий прийом, що й в оновленні.
            subprocess.Popen(
                ["bash", "-c", cmd],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            log_fh.close()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"не вдалось запустити видалення: {exc}") from exc

    return {
        "status": "uninstalling",
        "purge_data": purge_data,
        "message": (
            "Менеджер знімається — за кілька секунд він зникне з Applications"
            + (" разом із налаштуваннями" if purge_data else ", налаштування лишаються")
        ),
        "log": str(_UNINSTALL_LOG),
    }


# bhm.log is written next to the running app (APP_DIR) — the .exe's folder
# when frozen, the install root otherwise (which equals ~/.barhandler-manager
# for the Python install). boot/update logs are Python-install artifacts.
_LOG_SOURCES = {
    "bhm": APP_DIR / "bhm.log",
    "boot": _INSTALL_DIR / "bhm.boot.log",
    "update": _UPDATE_LOG,
}


@router.get("/logs")
async def read_log(source: str = "bhm", tail: int = 300) -> dict:
    """Return the last N lines of one of the manager's three log files.
    Dashboard surfaces these in a tabbed panel so the operator doesn't
    have to SSH in for routine diagnosis.

    - `bhm`    rotating app log (Python logger output: SSI flow, charges,
               errors)
    - `boot`   bhm.boot.log — stdout/stderr from the nohup-spawned
               process (uvicorn output, startup tracebacks, port-bind
               errors)
    - `update` ~/.barhandler-manager/update.log — what happened during
               the last dashboard-triggered update
    """
    path = _LOG_SOURCES.get(source)
    if path is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown log source '{source}' — pick one of {list(_LOG_SOURCES)}",
        )
    if not path.exists():
        return {"source": source, "path": str(path), "lines": [], "exists": False}
    tail = max(1, min(tail, 2000))
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"can't read {path}: {exc}") from exc
    lines = text.splitlines()[-tail:]
    return {"source": source, "path": str(path), "lines": lines, "exists": True}


@router.post("/usb-probe")
async def usb_probe() -> dict:
    """Run the standalone USB diagnostic and return its output.

    Tells the operator whether libusb sees the printer at all and
    whether it reports the standard USB Printer Class (0x07) we
    filter on. Replaces the curl-and-paste workflow for "manager
    can't find my printer" tickets.

    BH-158 — тіло винесене в `src.services.diagnostics`: та сама перевірка
    доступна і кнопкою в дашборді, і віддаленою командою з сервера логів, і
    це має бути ОДИН код. Доти тут була своя копія, яка шукала скрипт та
    інтерпретатор у `~/.barhandler-manager/.venv` — тобто в мак-застосунку
    й у вінді не працювала ніколи.
    """
    from src.services.diagnostics import run_diagnostic

    result = await run_diagnostic("usb_probe", {})
    if not result.get("ok") and not result.get("output"):
        raise HTTPException(
            status_code=500, detail=result.get("error", "usb probe failed"),
        )
    return {
        "exit_code": 0 if result.get("ok") else 1,
        "stdout": result.get("output", ""),
        "stderr": result.get("error", "") or "",
    }


_DEFAULT_UPLINK_URL = "https://manager.barhandler.com"
_ORIGIN_HOST_RE = re.compile(r"^https?://([a-zA-Z0-9.\-]{1,253})(:\d+)?$")


def _extract_tenant_from_origin(origin: str) -> Optional[str]:
    """`https://biergarten-lviv.barhandler.com` → `biergarten-lviv.barhandler.com`.
    Returns None for anything that doesn't look like a clean HTTPS origin."""
    m = _ORIGIN_HOST_RE.match(origin)
    return m.group(1) if m else None


class UplinkPayload(BaseModel):
    """POST /system/uplink body — just a toggle now. Tenant is auto-detected
    from the most recent PWA Origin seen by the manager (see server.py's
    `last_tenant_origin`); URL is hardcoded to manager.barhandler.com.
    Operator can override via direct config.yaml edit if they need a
    custom host."""
    enabled: bool


# BH-158 — РОБОЧІ дані, а не спакований ресурс: лише APP_DIR. Відносний до
# `__file__` шлях у мак-застосунку вказував у `_MEIPASS`, тож запис падав і
# перемикач uplink віддавав 500. Для скриптової інсталяції APP_DIR — це той
# самий корінь, що й раніше, тож поведінка не змінилась.
_CONFIG_PATH = APP_DIR / "config.yaml"

# Match an `uplink:` block (active or commented out) and consume all
# subsequent lines that belong to it: indented under `uplink:` (lines
# starting with whitespace), commented out (`#` lines), or blank. Stop at
# the next top-level non-comment key.
#
# Conservatively limited to the END of the file because `uplink:` is the
# last block by convention — the comment block immediately above
# `uplink:` (the documentation) is preserved by the leading anchor.
_UPLINK_BLOCK_RE = re.compile(
    r'(^|\n)(# Remote log uplink[^\n]*\n(?:#[^\n]*\n)*)?'
    r'(^|\n)(# )?uplink:[ \t]*\n'
    r'(?:(?:[ \t][^\n]*|#[^\n]*|[ \t]*)\n)*',
    re.MULTILINE,
)


def _render_uplink_block(
    enabled: bool,
    tenant: str,
    tenant_id: str = "",
    tenant_name: str = "",
    url: str = _DEFAULT_UPLINK_URL,
) -> str:
    # tenant_name may contain quotes/Cyrillic — YAML double-quoted scalar
    # only needs backslash + double-quote escaped.
    safe_name = tenant_name.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "# Remote log uplink — managed by the dashboard. Toggle via\n"
        "# POST /system/uplink. Editing this block by hand is fine, but the\n"
        "# UI overwrites the entire block on each save, so any comments you\n"
        "# add INSIDE the block will be lost on the next toggle.\n"
        "# Identity: install_id.txt is the stable key; tenant_id (appid) and\n"
        "# tenant_name are the auto-detected label of whoever is logged in.\n"
        "uplink:\n"
        f"  enabled: {'true' if enabled else 'false'}\n"
        f"  url: \"{url}\"\n"
        f"  tenant: \"{tenant}\"\n"
        f"  tenant_id: \"{tenant_id}\"\n"
        f"  tenant_name: \"{safe_name}\"\n"
        "  reconnect_delay: 2\n"
    )


def _replace_uplink_in_config(
    text: str,
    enabled: bool,
    tenant: str,
    tenant_id: str = "",
    tenant_name: str = "",
    url: str = _DEFAULT_UPLINK_URL,
) -> str:
    new_block = _render_uplink_block(enabled, tenant, tenant_id, tenant_name, url)
    m = _UPLINK_BLOCK_RE.search(text)
    if m:
        # Preserve a blank line before the new block if there was one.
        prefix = text[:m.start()].rstrip() + "\n\n"
        suffix = text[m.end():]
        return prefix + new_block + ("" if not suffix.strip() else suffix)
    # No existing block — append.
    base = text.rstrip() + "\n\n"
    return base + new_block


@router.get("/uplink")
async def get_uplink(request: Request) -> dict:
    state = request.app.state
    cfg = getattr(state, "config", {})
    uplink_cfg = cfg.get("uplink", {})
    client = getattr(state, "uplink", None)
    # `last_tenant_origin` is populated by the request middleware in
    # src/server.py whenever a non-localhost Origin hits the manager.
    last_origin = getattr(state, "last_tenant_origin", "")
    detected_tenant = _extract_tenant_from_origin(last_origin) if last_origin else None
    return {
        "enabled": bool(uplink_cfg.get("enabled", False)),
        "url": uplink_cfg.get("url", ""),
        "tenant": uplink_cfg.get("tenant", ""),
        "tenant_id": uplink_cfg.get("tenant_id", ""),
        "tenant_name": uplink_cfg.get("tenant_name", ""),
        "connected": bool(client and client.connected),
        "detected_tenant": detected_tenant,
        # Live identity from the most recent PWA ping — lets the dashboard
        # show who it will register as before the operator hits enable.
        "detected_tenant_id": getattr(state, "last_tenant_id", "") or None,
        "detected_tenant_name": getattr(state, "last_tenant_name", "") or None,
    }


@router.post("/uplink")
async def set_uplink(payload: UplinkPayload, request: Request) -> dict:
    """Toggle uplink on/off at runtime — no manager restart.

    Enabling: tenant is auto-detected from the most recent PWA Origin
    header. The config block is updated so the connection persists
    across restarts, and a `LogUplinkClient` is spun up RIGHT NOW —
    handler attached to root logger, socket.io connecting in the
    background.

    Disabling: stop the existing client (disconnect socket, detach log
    handler), clear the active singleton, write `enabled: false` to
    the config so a fresh boot doesn't reconnect.

    Both paths return synchronously — no SIGTERM, no respawn.
    """
    state = request.app.state
    cfg = getattr(state, "config", {})
    saved = cfg.get("uplink", {})

    if payload.enabled:
        # Preferred identity: explicit tenant headers captured from the
        # PWA (appid + display name). Fall back to a previously-saved
        # label, then to the legacy origin-derived subdomain. install_id
        # is the real key, so we never hard-fail on a missing label — it
        # fills in as soon as the app pings the manager once.
        tenant_id = getattr(state, "last_tenant_id", "") or saved.get("tenant_id", "")
        tenant_name = getattr(state, "last_tenant_name", "") or saved.get("tenant_name", "")
        last_origin = getattr(state, "last_tenant_origin", "")
        tenant = (
            (_extract_tenant_from_origin(last_origin) or "") if last_origin else ""
        ) or saved.get("tenant", "")
    else:
        tenant_id = saved.get("tenant_id", "")
        tenant_name = saved.get("tenant_name", "")
        tenant = saved.get("tenant", "")

    # Persist to config.yaml so the next boot reflects this state.
    try:
        # Файла може не бути: load_config() падає в дефолти в памʼяті, якщо
        # не змогла його створити. Відсутність — не привід валити перемикач:
        # блок uplink дописуємо в порожній текст, і файл зʼявляється тут.
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        text = (
            _CONFIG_PATH.read_text(encoding="utf-8")
            if _CONFIG_PATH.exists()
            else "server:\n  port: 9999\n"
        )
        new_text = _replace_uplink_in_config(
            text, payload.enabled, tenant,
            tenant_id=tenant_id, tenant_name=tenant_name,
        )
        _CONFIG_PATH.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"can't write {_CONFIG_PATH}: {exc}",
        ) from exc

    # Update the in-memory config so subsequent /system/uplink GETs
    # reflect the new state without a restart.
    cfg.setdefault("uplink", {})
    cfg["uplink"]["enabled"] = payload.enabled
    cfg["uplink"]["tenant"] = tenant
    cfg["uplink"]["tenant_id"] = tenant_id
    cfg["uplink"]["tenant_name"] = tenant_name
    cfg["uplink"]["url"] = _DEFAULT_UPLINK_URL

    # Runtime toggle — start or stop the LogUplinkClient in place.
    from src.services.log_uplink import (
        LogUplinkClient, get_or_create_install_id, set_active,
    )
    from src.services.diagnostics import make_callback

    existing = getattr(state, "uplink", None)

    if payload.enabled:
        # If a client is already there, just update its tenant (and
        # restart its socket so the handshake re-runs with the right
        # tenant), otherwise spin up a fresh one.
        if existing is not None:
            await existing.stop()
            existing.detach_handler_from_root()
        install_id_path = APP_DIR / "install_id.txt"
        install_id = get_or_create_install_id(install_id_path)
        version = installed_version()
        client = LogUplinkClient({
            "url": _DEFAULT_UPLINK_URL,
            "tenant": tenant,
            "tenant_id": tenant_id,
            "tenant_name": tenant_name,
            "reconnect_delay": 2,
        })
        client.attach_handler_to_root()
        client.set_diagnostics_callback(make_callback(cfg))
        set_active(client)
        state.uplink = client
        asyncio.create_task(client.start(install_id, version))
    else:
        if existing is not None:
            await existing.stop()
            existing.detach_handler_from_root()
        set_active(None)
        state.uplink = None

    return {
        "status": "saved",
        "message": (
            "uplink увімкнено" if payload.enabled else "uplink вимкнено"
        ),
        "uplink": {
            "enabled": payload.enabled,
            "tenant": tenant,
            "tenant_id": tenant_id,
            "tenant_name": tenant_name,
            "url": _DEFAULT_UPLINK_URL,
        },
    }


@router.get("/update-log")
async def read_update_log(tail: int = 200) -> dict:
    """Return the last N lines of update.log so the dashboard can show
    what happened to the last update attempt without making the operator
    SSH into the box. Cap at 1000 lines so a stuck loop can't fill the
    response."""
    if not _UPDATE_LOG.exists():
        return {"lines": [], "exists": False}
    tail = max(1, min(tail, 1000))
    # Read whole file (we cap log rotation elsewhere) and slice — simple
    # and avoids reverse-streaming complexity for a UI log.
    try:
        text = _UPDATE_LOG.read_text(errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"can't read update log: {exc}") from exc
    lines = text.splitlines()[-tail:]
    return {"lines": lines, "exists": True}
