import yaml
from pathlib import Path

# Install root is the parent of `src/` — that's where config.yaml lives.
# Resolving via __file__ instead of cwd means the manager works no matter
# where it's spawned from (launchctl bootstrap, runit, nohup from a
# shell script in $HOME, `python main.py` from anywhere).
_INSTALL_ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str = "config.yaml") -> dict:
    # Treat plain filenames as relative-to-install-root, but honour
    # absolute paths and cwd-relative paths the caller provides
    # explicitly (tests, alt-config setups).
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = _INSTALL_ROOT / p
    with open(p) as f:
        cfg = yaml.safe_load(f) or {}

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
