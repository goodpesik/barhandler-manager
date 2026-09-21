"""BH-161 — команда оновлення на вінді мусить і спрацьовувати, і звітувати.

У полі оновлення мовчки не відбулось, і розібратись не було по чому:
update.log містив лише наші власні заголовки. Це не збіг — на УСПІШНОМУ
шляху ні `Invoke-WebRequest -OutFile`, ні `Start-Process -Wait` не друкують
нічого, а код повернення ніде не зберігався. Лог виглядав однаково і коли
все вийшло, і коли інсталятор не запустився.
"""

import asyncio

from unittest.mock import patch

import pytest

from src.routes import system as system_routes


def _win_argv() -> tuple[list, str]:
    with patch.object(system_routes, "IS_WIN", True), \
         patch.object(system_routes, "FROZEN", True):
        return system_routes._build_update_argv()


def test_the_installer_is_opened_for_the_operator_not_run_silently():
    """Суть фікса. Беззвучний режим і зробив ваду невидимою: наш інсталятор
    не підписаний, файл із `Invoke-WebRequest` несе Mark-of-the-Web, і
    SmartScreen блокує таку пару діалогом. У беззвучному режимі той діалог
    невидимий — команда висить, у лозі ані рядка. Відкритий майстер показує
    і попередження, і запит прав людині, яка може їх натиснути.

    Так само поводиться мак-збірка зі своїм .pkg.
    """
    _argv, cmd = _win_argv()
    assert "/VERYSILENT" not in cmd, "беззвучний режим ховає діалог SmartScreen"
    assert "/SUPPRESSMSGBOXES" not in cmd
    assert "Start-Process -FilePath $tmp;" in cmd, "інсталятор має просто відкритись"


def test_the_command_does_not_wait_for_the_operator():
    """`-Wait` тут означав би «висіти рівно стільки, скільки людина не бачить
    майстра» — тобто нескінченно, якщо вона відійшла."""
    _argv, cmd = _win_argv()
    assert "-Wait" not in cmd


def test_the_mark_of_the_web_is_cleared_before_opening():
    """Мітку знімаємо все одно — менше причин для зайвого попередження."""
    _argv, cmd = _win_argv()
    assert "Unblock-File" in cmd
    assert cmd.index("Unblock-File") < cmd.index("Start-Process"), (
        "мітку треба знімати ДО відкриття, інакше вона не допоможе"
    )


def test_a_failure_leaves_a_message_not_silence():
    _argv, cmd = _win_argv()
    assert "$ErrorActionPreference = 'Stop'" in cmd, (
        "без Stop частина помилок не переривають скрипт і зникають"
    )
    assert "catch" in cmd and "FAILED" in cmd


def test_every_step_says_what_it_did():
    """Щоб наступний такий випадок читався з лога, а не вгадувався."""
    _argv, cmd = _win_argv()
    for marker in ("downloading", "downloaded", "opening installer", "installer opened"):
        assert marker in cmd, marker


def test_the_size_guard_survived():
    """Порожня чи обрізана закачка не має запускатись як інсталятор."""
    _argv, cmd = _win_argv()
    assert "-lt 100000" in cmd
    assert "too small" in cmd


def test_the_legacy_asset_name_is_still_the_one_fetched():
    """Копії в полі тягнуть адресу СВОЄЮ версією коду; ми міняємо лише те,
    що виконується тут. Актив лишається тим самим."""
    _argv, cmd = _win_argv()
    assert "device-handler-setup.exe" in cmd
    assert "/releases/latest/download/" in cmd


def test_non_windows_is_untouched():
    """Мак і скриптова інсталяція мають свої шляхи — їх ця зміна не чіпає."""
    with patch.object(system_routes, "IS_WIN", False), \
         patch.object(system_routes, "FROZEN", False), \
         patch.object(system_routes, "IS_MAC_APP_INSTALL", False):
        _argv, cmd = system_routes._build_update_argv()
    assert "Unblock-File" not in cmd
    assert "PowerShell" not in cmd and "powershell" not in cmd


def test_the_reply_stops_promising_an_automatic_restart_on_windows():
    """Текст «перезапуститься за ~30 секунд» став неправдою: тепер там
    відкривається майстер, і доки людина його не пройде, не зміниться нічого.
    Рівно та сама поправка, яку вже зробили для мака в BH-150."""
    with patch.object(system_routes, "IS_WIN", True), \
         patch.object(system_routes, "FROZEN", True), \
         patch.object(system_routes, "IS_MAC_APP_INSTALL", False):
        assert "інсталятор" in system_routes._update_started_message()
        assert "30 секунд" not in system_routes._update_started_message()


def test_the_script_install_still_promises_a_restart():
    """Скриптова інсталяція справді ставить усе сама — для неї текст правдивий."""
    with patch.object(system_routes, "IS_WIN", False), \
         patch.object(system_routes, "FROZEN", False), \
         patch.object(system_routes, "IS_MAC_APP_INSTALL", False):
        assert "30 секунд" in system_routes._update_started_message()


def test_the_powershell_text_stays_ascii():
    """Рядки, які PowerShell друкує в лог, мусять бути ASCII.

    Вінда з кодовою сторінкою 1251 перетворює тире й лапки на сміття — а це
    саме той лог, по якому потім розбирають, чому оновлення не пішло.
    Український текст лишається там, де його читає людина в інтерфейсі.
    """
    _argv, cmd = _win_argv()
    non_ascii = sorted({c for c in cmd if ord(c) > 127})
    assert non_ascii == [], f"не-ASCII у команді PowerShell: {non_ascii}"


# --- ознака для тих, хто викликає endpoint ---------------------------------


def _interactive(*, win: bool, frozen: bool, mac_app: bool) -> bool:
    with patch.object(system_routes, "IS_WIN", win), \
         patch.object(system_routes, "FROZEN", frozen), \
         patch.object(system_routes, "IS_MAC_APP_INSTALL", mac_app):
        return system_routes._update_is_interactive()


def test_opening_an_installer_is_reported_as_interactive():
    """Каса й дашборд мусять знати, що оновлення чекає на ЛЮДИНУ, — і знати це
    полем, а не розбором українського рядка. Вінда-збірка і мак-застосунок
    відкривають майстер; скриптова інсталяція ставить усе сама."""
    assert _interactive(win=True, frozen=True, mac_app=False) is True
    assert _interactive(win=False, frozen=True, mac_app=True) is True
    assert _interactive(win=False, frozen=False, mac_app=False) is False
    # Вінда БЕЗ frozen — це скриптова інсталяція, вона ставить сама.
    assert _interactive(win=True, frozen=False, mac_app=False) is False


def test_the_message_and_the_flag_cannot_disagree():
    """Текст і ознака беруться з одного джерела. Якби вони розʼїхались,
    інтерфейс казав би одне, а поводився за іншим."""
    for win, frozen, mac_app in [
        (True, True, False),
        (False, True, True),
        (False, False, False),
        (True, False, False),
    ]:
        with patch.object(system_routes, "IS_WIN", win), \
             patch.object(system_routes, "FROZEN", frozen), \
             patch.object(system_routes, "IS_MAC_APP_INSTALL", mac_app):
            interactive = system_routes._update_is_interactive()
            message = system_routes._update_started_message()
        assert ("інсталятор" in message) is interactive, (message, interactive)
        assert ("30 секунд" in message) is (not interactive), (message, interactive)


def test_the_endpoint_returns_the_flag(monkeypatch, tmp_path):
    """Ознака мусить доїхати до клієнта, а не лишитись у хелпері."""
    import asyncio

    class _FakePopen:
        def __init__(self, *a, **kw):
            spawned.append(a)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def poll(self):
            # BH-174 — жива дитина: маршрут питає код повернення, щоб не
            # звітувати «запущено» про процес, який уже помер.
            return None

    spawned: list = []
    # Патчимо ТІЛЬКИ Popen у модулі subprocess — і саме тому підміняємо
    # `_mac_host_arch`: він ходить у `subprocess.run`, який будує Popen сам.
    monkeypatch.setattr(system_routes.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(system_routes, "_mac_host_arch", lambda: "silicon")
    monkeypatch.setattr(system_routes, "_UPDATE_LOG", tmp_path / "update.log")

    from types import SimpleNamespace
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", True)
    body = asyncio.run(system_routes.trigger_update(request))
    assert spawned, "оновлення навіть не запустилось"
    assert body["interactive"] is True

    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", False)
    monkeypatch.setattr(system_routes, "IS_WIN", False)
    monkeypatch.setattr(system_routes, "FROZEN", False)
    body = asyncio.run(system_routes.trigger_update(request))
    assert body["interactive"] is False


# ─────────────────────────────────────────────────────────────────────────────
# BH-174 — оновлення на вінді не робило НІЧОГО й не лишало сліду.
#
# У полі `update.log` містив самі наші заголовки: ні `update: downloading`, ні
# `update: FAILED` — тобто PowerShell не доживав навіть до першого `Write-Host`.
# Причина не в SmartScreen (його ж лікували в BH-161, а мовчання лишилось), а в
# парі «`DETACHED_PROCESS` + консольний хост»: дитина лишалась зовсім без
# консолі. Плюс вивід і не міг дійти до лога — він тримався на успадкованому
# дескрипторі.
# ─────────────────────────────────────────────────────────────────────────────


def test_the_update_runs_through_cmd_and_writes_the_log_itself(tmp_path, monkeypatch):
    """Запуск іде через `cmd`, і саме `cmd` пише в лог (`>> "лог" 2>&1`)."""
    log = tmp_path / "Program Files" / "update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(system_routes, "_UPDATE_LOG", log)

    cmdline, _desc = _win_argv()

    assert isinstance(cmdline, str), (
        "на вінді команда мусить бути РЯДКОМ: список Python серіалізує через "
        "list2cmdline, яка екранує лапки бекслешем, а cmd такого не розуміє"
    )
    assert cmdline.startswith('cmd.exe /c "'), f"запуск не через cmd: {cmdline!r}"
    assert cmdline.endswith('"'), "немає зовнішньої пари лапок, яку знімає cmd /c"
    assert "-File" in cmdline, "тіло має йти файлом, а не рядком через -Command"
    assert f'>> "{log}" 2>&1' in cmdline, "перенаправлення в лог робить не cmd"
    # Найголовніше: жодного бекслеш-екранування лапок — саме воно й ламало шляхи.
    assert '\\"' not in cmdline, "лапки екрановані бекслешем — cmd їх не зрозуміє"


def test_paths_with_spaces_survive_the_command_line(tmp_path, monkeypatch):
    """«Program Files» і кирилиця в TEMP — звичайні шляхи на касі."""
    log = tmp_path / "Program Files" / "Device Handler" / "update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "Користувач Каса" / "update-cmd.ps1"
    script.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(system_routes, "_UPDATE_LOG", log)
    monkeypatch.setattr(system_routes, "_win_script_path", lambda: script)

    cmdline, _desc = _win_argv()

    # Кожен шлях — у своїй парі лапок, інакше пробіл розріже аргумент.
    assert f'-File "{script}"' in cmdline
    assert f'>> "{log}"' in cmdline


def test_the_body_lands_in_a_file_the_launcher_points_at(tmp_path, monkeypatch):
    """Те, що запускають, і те, що написали, — той самий файл."""
    monkeypatch.setattr(system_routes, "_UPDATE_LOG", tmp_path / "update.log")
    monkeypatch.setattr(
        system_routes, "_win_script_path", lambda: tmp_path / "update-cmd.ps1"
    )

    cmdline, desc = _win_argv()

    script = tmp_path / "update-cmd.ps1"
    assert script.exists(), "тіло оновлення не записалось у файл"
    assert str(script) in cmdline, "запускають не той файл, який написали"
    assert "device-handler-setup.exe" in script.read_text(encoding="utf-8")
    assert desc.startswith("$ErrorActionPreference"), "опис має лишитись тілом команди"


def test_the_child_keeps_a_console_so_powershell_survives(tmp_path, monkeypatch):
    """`DETACHED_PROCESS` забирає консоль, і `powershell.exe` без неї гине
    мовчки. Ховаємо вікно `CREATE_NO_WINDOW`, консоль лишаємо."""
    seen: dict = {}

    class _FakePopen:
        def __init__(self, *a, **kw):
            seen.update(kw)

        def poll(self):
            return None

    monkeypatch.setattr(system_routes.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(system_routes, "_UPDATE_LOG", tmp_path / "update.log")
    monkeypatch.setattr(
        system_routes, "_win_script_path", lambda: tmp_path / "update-cmd.ps1"
    )
    monkeypatch.setattr(system_routes, "IS_WIN", True)
    monkeypatch.setattr(system_routes, "FROZEN", False)
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", False)

    from types import SimpleNamespace

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    asyncio.run(system_routes.trigger_update(request))

    flags = seen.get("creationflags", 0)
    assert flags & system_routes.WIN_CREATE_NO_WINDOW, "вікно не приховане"
    assert not (flags & system_routes.WIN_DETACHED_PROCESS), (
        "DETACHED_PROCESS повернувся — саме він і вбивав PowerShell"
    )


def test_a_launcher_that_dies_at_once_is_not_reported_as_started(tmp_path, monkeypatch):
    """Мовчазна смерть виглядала точно як успішний старт: `Popen` не падає, а на
    процес ніхто не дивився. Тепер це помилка, і код повернення — у лозі."""
    log = tmp_path / "update.log"

    class _DeadPopen:
        def __init__(self, *a, **kw):
            pass

        def poll(self):
            return 9009  # «команду не знайдено» у cmd

    monkeypatch.setattr(system_routes.subprocess, "Popen", _DeadPopen)
    monkeypatch.setattr(system_routes, "_UPDATE_LOG", log)
    monkeypatch.setattr(
        system_routes, "_win_script_path", lambda: tmp_path / "update-cmd.ps1"
    )
    monkeypatch.setattr(system_routes, "IS_WIN", True)
    monkeypatch.setattr(system_routes, "FROZEN", False)
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", False)

    from types import SimpleNamespace

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    with pytest.raises(Exception) as err:
        asyncio.run(system_routes.trigger_update(request))

    assert "9009" in str(getattr(err.value, "detail", err.value))
    assert "rc=9009" in log.read_text(), "код повернення не потрапив у лог"


def test_each_attempt_writes_its_own_script(monkeypatch, tmp_path):
    """Кнопку тиснуть по кілька разів підряд (у полі було три за хвилину).
    Спільний файл означав би, що новий запис лягає під ноги тому PowerShell,
    який його ще читає."""
    monkeypatch.setattr(system_routes, "_UPDATE_LOG", tmp_path / "update.log")
    monkeypatch.setattr(
        system_routes.tempfile, "gettempdir", lambda: str(tmp_path)
    )

    first = system_routes._win_script_path()
    second = system_routes._win_script_path()

    assert first != second, "дві спроби пишуть в один файл"
    assert first.suffix == ".ps1" and second.suffix == ".ps1"


def test_the_log_handle_is_not_handed_to_the_child_on_windows(tmp_path, monkeypatch):
    """Вивід у лог пише сам `cmd`. Дескриптор дитині віддавати не можна: на
    вінді відкритий дескриптор не дає лог ні перейменувати, ні видалити."""
    seen: dict = {}

    class _FakePopen:
        def __init__(self, *a, **kw):
            seen.update(kw)

        def poll(self):
            return None

    monkeypatch.setattr(system_routes.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(system_routes, "_UPDATE_LOG", tmp_path / "update.log")
    monkeypatch.setattr(
        system_routes, "_win_script_path", lambda: tmp_path / "update-cmd.ps1"
    )
    monkeypatch.setattr(system_routes, "IS_WIN", True)
    monkeypatch.setattr(system_routes, "FROZEN", False)
    monkeypatch.setattr(system_routes, "IS_MAC_APP_INSTALL", False)
    monkeypatch.setattr(system_routes, "_DEAD_CHILD_GRACE_SECONDS", 0)

    from types import SimpleNamespace

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    asyncio.run(system_routes.trigger_update(request))

    import subprocess as sp

    assert seen.get("stdout") == sp.DEVNULL, "дитині віддали дескриптор лога"
    assert seen.get("stderr") == sp.DEVNULL


def test_the_grace_is_short_enough_not_to_hold_the_button(tmp_path):
    """Пауза перед перевіркою «чи жива дитина» стоїть у відповіді користувачу:
    завелика — кнопка висне, нульова — не відрізнить старт від смерті."""
    assert 0 < system_routes._DEAD_CHILD_GRACE_SECONDS <= 1
