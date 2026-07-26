import sys
import yaml
from pathlib import Path


def _app_dir() -> Path:
    """Base directory for config + persistent data (printers.json,
    terminals.json, logs).

    Source run: the install root (parent of `src/`), so the manager works
    no matter where it's spawned from (launchctl / systemd / nohup).

    Frozen (PyInstaller onefile exe): `__file__` lives inside the temp
    `_MEIPASS` extraction dir, which is WIPED on every run — so config and
    data must live next to the .exe instead, or they'd vanish each restart
    (and there'd be no config.yaml on first run → FileNotFoundError). We use
    the directory that contains the .exe.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


APP_DIR = _app_dir()
# Backwards-compatible alias — some call sites imported this name.
_INSTALL_ROOT = APP_DIR

# Minimal default written on first run when no config.yaml exists yet
# (fresh frozen-exe install). Everything else has sane runtime defaults.
_DEFAULT_CONFIG = "server:\n  port: 9999\n"


def load_config(path: str = "config.yaml") -> dict:
    # Treat plain filenames as relative-to-APP_DIR, but honour absolute /
    # cwd-relative paths the caller provides explicitly (tests, alt configs).
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = APP_DIR / p

    if not p.exists():
        # Fresh install (esp. the frozen exe on first launch): write a
        # minimal, operator-editable default next to the exe instead of
        # crashing. If the location is read-only (e.g. running from inside
        # _MEIPASS), fall back to in-memory defaults.
        try:
            p.write_text(_DEFAULT_CONFIG, encoding="utf-8")
        except OSError:
            return _finalize({})

    try:
        with open(p, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    return _finalize(cfg)


def _finalize(cfg: dict) -> dict:
    # `server.port` is indexed unconditionally in main.py — guarantee it.
    server = cfg.get("server") or {}
    server.setdefault("port", 9999)
    # For the frozen exe, default the data files next to the .exe so they
    # persist across restarts (source runs keep the historical cwd-relative
    # "printers.json"/"terminals.json" behaviour untouched).
    if getattr(sys, "frozen", False):
        server.setdefault("registry_path", str(APP_DIR / "printers.json"))
        server.setdefault("terminal_registry_path", str(APP_DIR / "terminals.json"))
    cfg["server"] = server

    # Uplink (remote log + diagnostics server) — config block is ALWAYS
    # populated with sane defaults so the dashboard can toggle the
    # connection at runtime without rewriting the file.
    #
    # Install identity on the logs server is keyed by the stable local
    # `install_id` UUID. The tenant fields below are the human-readable
    # label of WHO is logged in on this machine, auto-detected from the
    # PWA's X-Tenant-Id (appid) / X-Tenant-Name headers when the operator
    # clicks "Підключити". `tenant` is the legacy subdomain field, kept
    # for backward compatibility with older logs-server builds.
    uplink_cfg = (cfg.get("uplink") or {})
    cfg["uplink"] = {
        "enabled": bool(uplink_cfg.get("enabled", False)),
        "url": str(uplink_cfg.get("url") or "https://manager.barhandler.com"),
        "tenant": str(uplink_cfg.get("tenant", "")),
        "tenant_id": str(uplink_cfg.get("tenant_id", "")),
        "tenant_name": str(uplink_cfg.get("tenant_name", "")),
        "reconnect_delay": int(uplink_cfg.get("reconnect_delay", 2)),
    }
    # install_id alone is enough to reach the install from the logs
    # server, so an enabled uplink no longer HARD-requires a tenant label
    # (it fills in as soon as the PWA pings the manager). We keep a soft
    # invariant only for fully-legacy configs that still rely on `tenant`.
    return cfg
