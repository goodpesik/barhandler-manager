"""BH-161 — команда оновлення на вінді мусить і спрацьовувати, і звітувати.

У полі оновлення мовчки не відбулось, і розібратись не було по чому:
update.log містив лише наші власні заголовки. Це не збіг — на УСПІШНОМУ
шляху ні `Invoke-WebRequest -OutFile`, ні `Start-Process -Wait` не друкують
нічого, а код повернення ніде не зберігався. Лог виглядав однаково і коли
все вийшло, і коли інсталятор не запустився.
"""

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
