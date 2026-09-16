"""BH-158 — робочі дані живуть в APP_DIR, а не поруч із кодом.

Вада, яку тут ловлять: шлях до файла, який треба ПЕРЕЖИТИ перезапуск,
рахувався від `__file__`. У скриптовій інсталяції це корінь інсталяції, тож
воно працювало; у замороженій збірці (мак-застосунок, вінда-exe) — тимчасова
тека `_MEIPASS`, яку стирають на кожному запуску. Наслідки в полі:
POST /system/uplink віддавав 500, install_id народжувався новий на кожен
рестарт, віддалена діагностика не бачила ні лог, ні термінали.

Перший тест — на КІЛЬКІСТЬ місць: він падає, щойно хтось додасть НОВУ копію
цієї вади в будь-якому файлі репозиторію.
"""

import ast
import re
from pathlib import Path

import pytest

from src.config import APP_DIR

_SRC = Path(__file__).resolve().parent.parent / "src"

# Файли, які мусять пережити перезапуск. Ресурсів, спакованих у бандл
# (VERSION, assets/fonts, scripts/) тут свідомо немає — їх якраз і треба
# адресувати відносно `__file__`.
_RUNTIME_FILES = (
    "config.yaml",
    "install_id.txt",
    "printers.json",
    "terminals.json",
    "bhm.log",
)


def _module_sources() -> list[tuple[Path, str]]:
    return [(p, p.read_text(encoding="utf-8")) for p in sorted(_SRC.rglob("*.py"))]


def _code_rooted_names(tree: ast.AST) -> set[str]:
    """Імена модульного рівня, які тримають шлях, похідний від `__file__`
    (наприклад `_BUNDLE_ROOT = Path(__file__).resolve().parent.parent`).

    Без цього кроку тест ловив би лише буквальний `Path(__file__) / "x"`, а
    той самий дефект, схований за псевдонімом кореня, проходив би повз —
    саме на цьому перша редакція тесту й пройшла мутацію.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and "__file__" in ast.dump(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def test_no_runtime_file_is_addressed_relative_to_source_file():
    """Жоден робочий файл не склеюється зі шляхом, похідним від `__file__` —
    ні напряму, ні через псевдонім кореня коду.

    Шукаємо у дереві розбору, а не текстом: щоб не ловити згадки в коментарях
    і щоб зміна кількості `.parent` не пройшла повз.
    """
    offenders: list[str] = []
    for path, source in _module_sources():
        tree = ast.parse(source, filename=str(path))
        rooted = _code_rooted_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
                continue
            if not isinstance(node.right, ast.Constant) or node.right.value not in _RUNTIME_FILES:
                continue
            left = ast.dump(node.left)
            rooted_alias = any(
                isinstance(n, ast.Name) and n.id in rooted for n in ast.walk(node.left)
            )
            if "__file__" in left or rooted_alias:
                offenders.append(
                    f"{path.relative_to(_SRC.parent)}:{node.lineno} → {node.right.value}",
                )
    assert offenders == [], (
        "робочі дані адресовані відносно коду замість APP_DIR: " + "; ".join(offenders)
    )


def test_bundled_resources_are_still_addressed_relative_to_code():
    """Зворотний бік тієї самої монети: VERSION і шрифти НЕ можна переносити
    в APP_DIR — вони їдуть у бандлі, і в замороженій збірці лежать у
    `_MEIPASS`. Якщо хтось «причеше» і їх, версія стане 0.0.0."""
    version_refs = [
        (path, source)
        for path, source in _module_sources()
        if '/ "VERSION"' in source
    ]
    assert version_refs, "очікували посилання на VERSION у src/"
    for path, source in version_refs:
        for line in source.splitlines():
            if '/ "VERSION"' in line:
                assert "__file__" in line, f"{path}: VERSION має резолвитись від коду"


def test_config_path_is_under_app_dir():
    from src.routes.system import _CONFIG_PATH

    assert _CONFIG_PATH == APP_DIR / "config.yaml"


def test_diagnostics_log_and_terminals_follow_app_dir(monkeypatch, tmp_path):
    """Доводимо саме ЗАЛЕЖНІСТЬ від APP_DIR, а не збіг шляхів.

    У вихідному чекауті APP_DIR дорівнює кореню коду, тож порівняння
    `шлях == APP_DIR / "bhm.log"` проходить і з поверненою вадою. Підміняємо
    APP_DIR на теку, якої немає ніде більше: якщо код бере корінь від
    `__file__`, тест червоніє.
    """
    from src.services import diagnostics

    monkeypatch.setattr(diagnostics, "APP_DIR", tmp_path)
    assert diagnostics._bhm_log_path() == tmp_path / "bhm.log"
    # Без конфіга — дефолт з APP_DIR, а не з поточної теки процесу.
    assert diagnostics._terminals_path(None) == tmp_path / "terminals.json"
    assert diagnostics._terminals_path({}) == tmp_path / "terminals.json"
    # Відносний шлях із конфіга — теж від APP_DIR, не від cwd і не від коду.
    assert diagnostics._terminals_path(
        {"server": {"terminal_registry_path": "sub/terminals.json"}},
    ) == tmp_path / "sub" / "terminals.json"


def test_diagnostics_terminals_follows_the_configured_path():
    """Діагностика мусить читати ТОЙ САМИЙ файл, який веде TerminalRegistry."""
    from src.services import diagnostics

    assert diagnostics._terminals_path(
        {"server": {"terminal_registry_path": "/tmp/bh-terminals.json"}},
    ) == Path("/tmp/bh-terminals.json")


@pytest.mark.asyncio
async def test_tail_log_reads_the_file_under_app_dir(monkeypatch, tmp_path):
    """Поведінковий бік: підтримка натискає «показати лог» і мусить побачити
    вміст, а не «bhm.log not found»."""
    from src.services import diagnostics

    monkeypatch.setattr(diagnostics, "APP_DIR", tmp_path)
    (tmp_path / "bhm.log").write_text("рядок-один\nрядок-два\n", encoding="utf-8")
    r = await diagnostics.run_diagnostic("tail_log", {"n": 1})
    assert r["ok"] is True
    assert r["output"] == "рядок-два"


@pytest.mark.asyncio
async def test_list_terminals_reads_the_configured_file(tmp_path):
    from src.services.diagnostics import run_diagnostic

    registry = tmp_path / "terminals.json"
    registry.write_text('[{"id": "abc"}]', encoding="utf-8")
    r = await run_diagnostic(
        "list_terminals", {},
        config={"server": {"terminal_registry_path": str(registry)}},
    )
    assert r["ok"] is True
    assert "abc" in r["output"]


def test_app_dir_for_a_frozen_mac_app_is_not_the_bundle(monkeypatch):
    """Головна причина 500: у мак-застосунку код лежить у теці, яку стирають.
    APP_DIR мусить вести в домівку, і саме туди — в те саме місце, що й
    скриптова інсталяція."""
    import sys

    import src.config as config_mod

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert config_mod._app_dir() == Path.home() / ".barhandler-manager"


def test_app_dir_for_a_frozen_windows_exe_sits_next_to_the_exe(monkeypatch, tmp_path):
    import sys

    import src.config as config_mod

    exe = tmp_path / "bhm.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(exe))
    assert config_mod._app_dir() == tmp_path.resolve()


def test_usb_probe_script_ships_in_the_frozen_bundle():
    """Скрипт USB-перевірки — спакований ресурс. Якщо його не класти в
    datas, у замороженій збірці кнопка й віддалена команда шукатимуть
    неіснуючий файл."""
    spec = (Path(__file__).resolve().parent.parent / "bhm.spec").read_text(encoding="utf-8")
    assert re.search(r'datas \+= \[\("scripts/usb_probe\.py", "scripts"\)\]', spec)


def test_uplink_toggle_survives_a_missing_config_file(client, auth_headers, monkeypatch, tmp_path):
    """Відтворення самої скарги: у мак-застосунку `POST /system/uplink`
    віддавав 500, бо писав у `_MEIPASS`, де config.yaml немає.

    Тут те саме в мініатюрі — шлях веде в теку, де файла ще нема. Перемикач
    мусить не впасти, а створити файл і записати блок uplink.
    """
    import yaml

    from src.routes import system as system_routes

    cfg_path = tmp_path / "nested" / "config.yaml"
    monkeypatch.setattr(system_routes, "_CONFIG_PATH", cfg_path)

    r = client.post("/system/uplink", json={"enabled": False}, headers=auth_headers)

    assert r.status_code == 200, r.text
    assert cfg_path.exists()
    parsed = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert parsed["uplink"]["enabled"] is False
    assert parsed["server"]["port"] == 9999


def test_inprocess_script_runner_captures_the_scripts_print(tmp_path):
    from src.services.diagnostics import _run_script_inprocess

    script = tmp_path / "probe.py"
    script.write_text(
        "print('рядок один')\nprint('a', 'b', sep='-')\n",
        encoding="utf-8",
    )
    ok, out = _run_script_inprocess(script)
    assert ok is True
    assert out == "рядок один\na-b\n"


def test_inprocess_script_runner_leaves_global_stdout_alone_WHILE_it_runs(tmp_path):
    """Суть вимоги — не чіпати `sys.stdout` САМЕ ПІД ЧАС виконання.

    Перевірка «після виклику sys.stdout той самий» нічого не доводить:
    `contextlib.redirect_stdout` повертає його на місце на виході, тож така
    перевірка пройшла б і з глобальною підміною. Тому дивимось зсередини:
    запис ПРЯМО в `sys.stdout` під час прогону не має потрапити в наш
    результат — інакше туди ж падали б і рядки чужих запитів, які в цю
    секунду щось друкують.
    """
    from src.services.diagnostics import _run_script_inprocess

    script = tmp_path / "probe.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('ЧУЖИЙ-РЯДОК\\n')\n"
        "print('наш рядок')\n",
        encoding="utf-8",
    )
    ok, out = _run_script_inprocess(script)
    assert ok is True
    assert "наш рядок" in out
    assert "ЧУЖИЙ-РЯДОК" not in out


def test_inprocess_script_runner_reports_a_nonzero_exit_as_failure(tmp_path):
    from src.services.diagnostics import _run_script_inprocess

    script = tmp_path / "probe.py"
    script.write_text("import sys\nprint('libusb missing')\nsys.exit(1)\n", encoding="utf-8")
    ok, out = _run_script_inprocess(script)
    assert ok is False
    assert "libusb missing" in out


def test_inprocess_script_runner_does_not_let_a_crash_escape(tmp_path):
    """Діагностика не має валити менеджер — навіть якщо скрипт кинув."""
    from src.services.diagnostics import _run_script_inprocess

    script = tmp_path / "probe.py"
    script.write_text("print('до')\nraise RuntimeError('бум')\n", encoding="utf-8")
    ok, out = _run_script_inprocess(script)
    assert ok is False
    assert "до" in out and "RuntimeError: бум" in out


@pytest.mark.asyncio
async def test_frozen_usb_probe_gives_up_instead_of_hanging(monkeypatch, tmp_path):
    """Зависла перевірка не має тримати HTTP-запит вічно."""
    import asyncio

    from src.services import diagnostics

    script = tmp_path / "probe.py"
    script.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(diagnostics, "FROZEN", True)
    monkeypatch.setattr(diagnostics, "_usb_probe_script", lambda: script)
    monkeypatch.setattr(diagnostics, "_CMD_TIMEOUT", 0.05)

    async def _never(*_args, **_kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(diagnostics.asyncio, "to_thread", _never)

    r = await diagnostics.run_diagnostic("usb_probe", {})
    assert r["ok"] is False
    assert "timeout" in r["error"]


def test_inprocess_script_runner_handles_a_text_sys_exit(tmp_path):
    """Знайдено ревʼю: `sys.exit("текст")` валив сам обробник винятку.

    `int(exc.code)` на рядку кидало ValueError усередині `except SystemExit`,
    тобто повз `except Exception`, і виняток тікав аж у сокет-колбек — рівно
    те, чого ця функція мала б не допускати.
    """
    from src.services.diagnostics import _run_script_inprocess

    script = tmp_path / "probe.py"
    script.write_text("import sys\nsys.exit('libusb не знайдено')\n", encoding="utf-8")
    ok, out = _run_script_inprocess(script)
    assert ok is False
    assert "libusb не знайдено" in out


def test_inprocess_script_runner_treats_a_bare_exit_as_success(tmp_path):
    from src.services.diagnostics import _run_script_inprocess

    script = tmp_path / "probe.py"
    script.write_text("import sys\nprint('усе добре')\nsys.exit()\n", encoding="utf-8")
    ok, out = _run_script_inprocess(script)
    assert ok is True
    assert "усе добре" in out


def test_inprocess_script_runner_survives_an_exit_object_whose_str_raises(tmp_path):
    """Знайдено ДРУГИМ колом ревʼю — та сама вада, що й `sys.exit("текст")`,
    лише іншим входом.

    `f"{exc.code}"` усередині `except SystemExit` кликало `str()` на чужому
    обʼєкті. Якщо той `__str__` кидає, новий виняток народжується вже В
    обробнику — сусідній `except Exception` його не ловить — і він тікає з
    діагностики аж у виклик.
    """
    from src.services.diagnostics import _run_script_inprocess

    script = tmp_path / "probe.py"
    script.write_text(
        "import sys\n"
        "class Weird:\n"
        "    def __str__(self):\n"
        "        raise RuntimeError('bang')\n"
        "print('до виходу')\n"
        "sys.exit(Weird())\n",
        encoding="utf-8",
    )
    ok, out = _run_script_inprocess(script)
    assert ok is False
    assert "до виходу" in out
    assert "Weird" in out


def test_inprocess_script_runner_survives_an_exception_whose_str_raises(tmp_path):
    """Те саме на другій гілці — `except Exception`. Цей вхід реалістичніший:
    досить звичайного власного винятку, чий `__str__` лізе до атрибута,
    якого ще немає."""
    from src.services.diagnostics import _run_script_inprocess

    script = tmp_path / "probe.py"
    script.write_text(
        "class Broken(Exception):\n"
        "    def __str__(self):\n"
        "        raise ValueError('bad repr')\n"
        "print('до падіння')\n"
        "raise Broken()\n",
        encoding="utf-8",
    )
    ok, out = _run_script_inprocess(script)
    assert ok is False
    assert "до падіння" in out
    assert "Broken" in out


def test_inprocess_script_runner_treats_explicit_exit_zero_as_success(tmp_path):
    """Межа «успіх/помилка» на гілці int. Перевірка на хибність (`exc.code or 0`)
    замість `isinstance` пройшла б тест на голий `sys.exit()`, але зламала б
    саме цей випадок."""
    from src.services.diagnostics import _run_script_inprocess

    script = tmp_path / "probe.py"
    script.write_text("import sys\nprint('усе гаразд')\nsys.exit(0)\n", encoding="utf-8")
    ok, out = _run_script_inprocess(script)
    assert ok is True
    assert "усе гаразд" in out


def test_inprocess_script_runner_treats_a_nonzero_int_exit_as_failure(tmp_path):
    from src.services.diagnostics import _run_script_inprocess

    script = tmp_path / "probe.py"
    script.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    ok, _out = _run_script_inprocess(script)
    assert ok is False
