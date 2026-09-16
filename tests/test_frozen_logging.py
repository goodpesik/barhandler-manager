"""BH-159 — логування в замороженій збірці, і чистота access-лога.

Вада: бібліотека (`escpos.capabilities`) на ІМПОРТІ кличе
`logging.basicConfig()`, а `StreamHandler` запамʼятовує потік у момент
створення. У замороженій windowed-збірці `sys.stdout`/`sys.stderr` — None,
тож хендлер тримає None назавжди й валить КОЖЕН запис у лог. У клієнта це
дало 38 трейсбеків у власному ж лозі.
"""

import io
import logging
import re
from pathlib import Path

import pytest

_MAIN = Path(__file__).resolve().parent.parent / "main.py"


def _main_source() -> str:
    return _MAIN.read_text(encoding="utf-8")


def test_stdout_guard_comes_before_any_logging_import():
    """Порядок тут — це і є фікс.

    Підміна мусить стояти ДО `import logging` і до будь-якого імпорту, що
    тягне бібліотеки: інакше хендлер уже створений із мертвим потоком.
    Раніше вона жила в `if __name__ == "__main__"`, тобто після всього.
    """
    src = _main_source()
    guard = src.index("if _sys.stdout is None or _sys.stderr is None:")
    logging_import = src.index("import logging")
    server_import = src.index("from src.server import create_app")
    assert guard < logging_import, "підміна stdout після import logging — запізно"
    assert guard < server_import, "підміна stdout після імпорту сервера — запізно"


def test_stdout_guard_is_not_hidden_in_main_block():
    """Мутація, яку це ловить: повернути захист під `if __name__`."""
    src = _main_source()
    guard = src.index("if _sys.stdout is None or _sys.stderr is None:")
    main_block = src.index('if __name__ == "__main__":')
    assert guard < main_block


def test_dead_stream_detector_recognises_the_broken_handler():
    from main import _is_dead_stream

    dead = logging.StreamHandler()
    dead.stream = None
    assert _is_dead_stream(dead) is True

    closed = logging.StreamHandler(io.StringIO())
    closed.stream.close()
    assert _is_dead_stream(closed) is True

    alive = logging.StreamHandler(io.StringIO())
    assert _is_dead_stream(alive) is False


def _access_record(client: str, method: str, path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(client, method, path, "1.1", status),
        exc_info=None,
    )


@pytest.mark.parametrize("path", ["/", "/health", "/devices", "/terminal", "/version", "/system/uplink"])
def test_dashboard_polling_is_kept_out_of_the_log(path):
    from main import _DashboardPollFilter

    f = _DashboardPollFilter()
    assert f.filter(_access_record("127.0.0.1:5000", "GET", path, 200)) is False


def test_everything_worth_reading_still_reaches_the_log():
    """Фільтр мусить бути ВУЗЬКИМ. Кожен рядок тут — те, заради чого лог і
    читають; якщо хоч один зникне, віддалена діагностика осліпне."""
    from main import _DashboardPollFilter

    f = _DashboardPollFilter()
    keep = [
        ("127.0.0.1:5000", "GET", "/health", 500),      # помилка
        ("127.0.0.1:5000", "POST", "/print/receipt", 200),  # не GET
        ("192.168.1.50:5000", "GET", "/health", 200),   # чужа адреса
        ("127.0.0.1:5000", "GET", "/print/label", 200), # не полінгова адреса
        ("127.0.0.1:5000", "GET", "/devices/register", 200),
    ]
    for client, method, path, status in keep:
        assert f.filter(_access_record(client, method, path, status)) is True, (
            f"фільтр зʼїв те, що мав лишити: {method} {path} {status} від {client}"
        )


def test_polling_filter_survives_a_record_it_does_not_understand():
    """Чужий рядок у тому ж логері не має валити фільтр — інакше ми міняємо
    шумний лог на відсутній."""
    from main import _DashboardPollFilter

    f = _DashboardPollFilter()
    plain = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "просто рядок", None, None,
    )
    assert f.filter(plain) is True
    weird = _access_record("127.0.0.1:5000", "GET", "/health", 200)
    weird.args = ("лише один аргумент",)
    assert f.filter(weird) is True


@pytest.mark.asyncio
async def test_tail_log_rejects_an_unknown_argument():
    """Підтримка передала `lines` замість `n` і мовчки отримала дефолт —
    тобто неправду про обсяг лога."""
    from src.services.diagnostics import run_diagnostic

    r = await run_diagnostic("tail_log", {"lines": 4000})
    assert r["ok"] is False
    assert "lines" in r["error"]


@pytest.mark.asyncio
async def test_tail_log_still_accepts_its_own_argument(monkeypatch, tmp_path):
    from src.services import diagnostics

    monkeypatch.setattr(diagnostics, "APP_DIR", tmp_path)
    (tmp_path / "bhm.log").write_text("a\nb\nc\n", encoding="utf-8")
    r = await diagnostics.run_diagnostic("tail_log", {"n": 2})
    assert r["ok"] is True
    assert r["output"] == "b\nc"
