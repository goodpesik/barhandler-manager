import logging
import logging.handlers
from pathlib import Path

import uvicorn

from src.config import load_config
from src.server import create_app

config = load_config()


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

    log_path = Path(__file__).resolve().parent / "bhm.log"
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
    for existing in list(root.handlers):
        if isinstance(existing, logging.handlers.RotatingFileHandler):
            root.removeHandler(existing)
    root.addHandler(handler)

    # Uvicorn ships its own formatters that ignore the root config — re-route
    # them to the same rotating file so we don't lose request logs.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False


_configure_logging(config)

app = create_app(config)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=config["server"]["port"])
