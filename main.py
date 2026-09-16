# BH-159 — ЦЕЙ БЛОК МУСИТЬ ЛИШАТИСЬ ПЕРШИМ У ФАЙЛІ.
#
# У замороженій windowed-збірці (console=False: мак-застосунок і вінда-exe)
# `sys.stdout` і `sys.stderr` дорівнюють None. Бібліотеки, які на ІМПОРТІ
# кличуть `logging.basicConfig()` — у нас це `escpos.capabilities` — чіпляють
# на root `StreamHandler`, а той запамʼятовує потік У МОМЕНТ СТВОРЕННЯ. Якщо
# на той час stdout ще None, хендлер тримає None назавжди й падає на КОЖНОМУ
# записі: у клієнта на вінді це дало 38 трейсбеків `AttributeError: 'NoneType'
# object has no attribute 'write'` у власному ж лозі.
#
# Тому підміну робимо ДО будь-якого імпорту, що може налаштувати логування.
# Раніше вона стояла в `if __name__ == "__main__"` — тобто вже після імпортів,
# і не встигала.
import sys as _sys

if _sys.stdout is None or _sys.stderr is None:
    # Знайдено ревʼю: голий `open()` тут — гірша вада, ніж та, яку він лікує.
    # Тека даних може бути незаписуваною (антивірус тримає файл, диск повний,
    # том лише на читання), і тоді виняток летить на самому верху модуля,
    # ДО будь-якої обробки помилок. У windowed-збірці це абсолютна тиша:
    # ні консолі, ні діалогу, ні лога — застосунок просто не стартує.
    #
    # Тому: пробуємо файл, не вийшло — беремо /dev/null. Логування без
    # консолі ми все одно не втрачаємо (є ротаційний файловий хендлер), а
    # головне — жоден бібліотечний `StreamHandler` не отримає None.
    try:
        from src.config import APP_DIR as _APP_DIR

        _devlog = open(_APP_DIR / "bhm.log", "a", encoding="utf-8")
    except Exception:  # noqa: BLE001 — старт важливіший за цей файл
        import os as _os

        _devlog = open(_os.devnull, "w", encoding="utf-8")
    if _sys.stdout is None:
        _sys.stdout = _devlog
    if _sys.stderr is None:
        _sys.stderr = _devlog

import logging
import logging.handlers
import os
from pathlib import Path

# macOS Python installs (Apple CLT, some Homebrew variants) ship without
# a usable CA bundle, so aiohttp / requests / ssl module all fail with
# `SSL: CERTIFICATE_VERIFY_FAILED unable to get local issuer certificate`
# on every HTTPS call. Point them at certifi's Mozilla bundle, which is
# already a transitive dep via httpx. setdefault keeps any operator-set
# override (e.g. corporate CA path).
try:
    import certifi as _certifi
    os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _certifi.where())
except ImportError:
    pass

import uvicorn

from src.config import APP_DIR, load_config
from src.server import create_app

config = load_config()


class _DashboardPollFilter(logging.Filter):
    """Відкидає рядки access-лога про власний полінг дашборда з локалхоста.

    Формат рядка uvicorn: `%s - "%s %s HTTP/%s" %d` з аргументами
    (client_addr, method, path, http_version, status). Спираємось на args, а
    не на готовий текст: формат може змінитись, а порядок аргументів — ні.

    Фільтруємо ЛИШЕ успішні GET на опитувані дашбордом адреси й лише з
    локалхоста. Помилка, чужа адреса чи будь-який інший метод проходять.
    """

    _POLLED = frozenset({
        "/", "/health", "/devices", "/terminal", "/version", "/system/uplink",
    })
    _LOCAL = ("127.0.0.1", "::1", "localhost")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        client, method, path, _http_version, status = args[:5]
        if not isinstance(client, str) or not client.startswith(self._LOCAL):
            return True
        if method != "GET":
            return True
        # Тільки 200. Знайдено ревʼю: «усе, що менше за 400» тихо ковтало б
        # і 301/304 на тих самих адресах, а редирект чи несподіваний 304 —
        # це вже не шум, а те, заради чого лог і читають.
        try:
            if int(status) != 200:
                return True
        except (TypeError, ValueError):
            return True
        return str(path).split("?", 1)[0] not in self._POLLED


def _is_dead_stream(handler: logging.StreamHandler) -> bool:
    """Чи тримає цей хендлер потік, у який уже не можна писати.

    Такий хендлер лишає `logging.basicConfig()`, викликаний бібліотекою на
    імпорті: він запамʼятовує `sys.stderr` У МОМЕНТ СТВОРЕННЯ, і в замороженій
    windowed-збірці це може бути None. Кожен запис у лог тоді перетворюється
    на `--- Logging error ---` із трейсбеком.
    """
    stream = getattr(handler, "stream", None)
    if stream is None:
        return True
    if getattr(stream, "closed", False):
        return True
    return not callable(getattr(stream, "write", None))


def _configure_logging(cfg: dict) -> None:
    """Bind every logger (ours + uvicorn + fastapi) to a rotating file
    so a busy week of receipts doesn't fill the disk.

    Defaults: 5 MB per file, 5 rollovers (so ~25 MB ceiling). Operators
    on a constrained Pi can override either via `logging.max_bytes` /
    `logging.backup_count` in `config.yaml`.

    Logs land next to `main.py` as `bhm.log` regardless of where the
    process was launched from — the OS service launchers
    (launchd / systemd / Termux services) already pass cwd, so this
    keeps logs co-located with `config.yaml` for easy support.
    """
    logging_cfg = cfg.get("logging") or {}
    max_bytes = int(logging_cfg.get("max_bytes", 5 * 1024 * 1024))
    backup_count = int(logging_cfg.get("backup_count", 5))
    level = logging_cfg.get("level", "INFO").upper()

    log_path = APP_DIR / "bhm.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ),
    )

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid double-handlers on uvicorn reload — strip ours if it's already attached.
    #
    # BH-159 — заодно знімаємо ЧУЖІ StreamHandler'и з мертвим потоком. Такий
    # хендлер лишає `logging.basicConfig()`, викликаний бібліотекою на імпорті;
    # у замороженій збірці його потік може бути None або вже закритим, і тоді
    # кожен запис у лог перетворюється на трейсбек. Блок нагорі файла робить
    # так, щоб мертвих не було взагалі, — це друга лінія на випадок, коли
    # хендлер зʼявився якимось іншим шляхом.
    for existing in list(root.handlers):
        if isinstance(existing, logging.handlers.RotatingFileHandler):
            root.removeHandler(existing)
        elif isinstance(existing, logging.StreamHandler) and _is_dead_stream(existing):
            root.removeHandler(existing)
    root.addHandler(handler)

    # Uvicorn ships its own formatters that ignore the root config — re-route
    # them to the same rotating file so we don't lose request logs.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False

    # BH-159 — дашборд полить сам себе пʼятьма GET кожні 2 секунди. У лозі це
    # ~150 рядків на хвилину, і будь-яка подія, старша за півтори хвилини,
    # витісняється з типового «останні 200 рядків». Віддалена діагностика
    # через це перетворюється на археологію: щоб дістати вікно в півтори
    # години, підтримці знадобилось n=4000.
    #
    # Прибираємо з access-лога САМЕ цей шум — свої ж опитування дашборда з
    # локалхоста. Усе інше (чужі адреси, помилки, будь-який метод, крім GET)
    # лишається: людині, що дивиться лог, потрібні саме вони.
    logging.getLogger("uvicorn.access").addFilter(_DashboardPollFilter())


_configure_logging(config)

app = create_app(config)

if __name__ == "__main__":
    # When launched windowless (Windows `pythonw.exe`, or any detached
    # service with no console) `sys.stdout` / `sys.stderr` are None.
    # Uvicorn's default log config and any stray `print` would then crash
    # on write, so route them to the rotating log file before use.
    import sys

    # Frozen mac .app: register a login autostart LaunchAgent. The .pkg
    # postinstall already does it, but a bundle copied by hand (or left over
    # from the old .dmg) has nobody to. No-op on Windows/source runs, and it
    # never takes over an autostart that points at a curl|bash install.
    from src.services.mac_autostart import ensure_launch_agent
    ensure_launch_agent()

    # BH-150 — перший запуск мак-збірки відкриває дашборд, щоб було видно, що
    # вона встановилась і працює: іконки в доку немає, вікна немає, і без цього
    # людина не має жодної ознаки життя. Один раз за життя інсталяції.
    from src.services.first_run import open_dashboard_once
    open_dashboard_once(APP_DIR, config["server"]["port"])

    # log_config=None keeps the file handlers wired up in
    # `_configure_logging` instead of letting uvicorn reinstall its own
    # stdout/stderr handlers (which would crash under pythonw).
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=config["server"]["port"],
        log_config=None,
    )
