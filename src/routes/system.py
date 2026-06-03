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
import re
import signal
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()

_INSTALL_DIR = Path.home() / ".barhandler-manager"
_UPDATE_LOG = _INSTALL_DIR / "update.log"


@router.get("/version")
async def get_version() -> dict:
    version_file = Path("VERSION")
    version = version_file.read_text().strip() if version_file.exists() else "unknown"
    return {"version": version}


@router.post("/update")
async def trigger_update() -> dict:
    """Spawn update.sh in a new session (detached from the manager process)
    so it survives the manager restart it triggers. A 2-second sleep gives
    the HTTP response time to reach the browser before the process dies.

    stdout+stderr go to update.log (NOT DEVNULL) so when the update fails
    silently — curl can't reach GitHub, launchctl refuses the reload,
    install.sh errors out on a missing dep — the operator has something
    to read instead of a frozen "Перезапуск…" button. Append-mode so a
    failed update doesn't wipe the previous attempt's trail.
    """
    script = _INSTALL_DIR / "update.sh"
    if not script.exists():
        # Fallback: inline the update command directly.
        cmd = (
            "sleep 2 && curl -fsSL "
            "https://github.com/goodpesik/barhandler-manager"
            "/releases/latest/download/install.sh | bash -s -- --force"
        )
    else:
        cmd = f"sleep 2 && bash {script}"

    try:
        _INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        # Append header so the operator can tell separate attempts apart
        # in update.log when they bang the button multiple times.
        with _UPDATE_LOG.open("a") as fh:
            fh.write(
                f"\n=== update triggered {_dt.datetime.now().isoformat()} "
                f"(pid={os.getpid()}) ===\n",
            )
            fh.write(f"cmd: {cmd}\n")
            fh.flush()
        # When the manager runs under launchd / systemd the inherited
        # PATH is the bare service-context one and doesn't include
        # Homebrew. install.sh itself prepends those prefixes now, but
        # set a sane PATH here too so we're not relying solely on the
        # downstream script — anything that runs before install.sh
        # sources its own PATH still resolves brew/python3.
        env = {
            **os.environ,
            "PATH": (
                "/opt/homebrew/bin:/opt/homebrew/sbin:"
                "/usr/local/bin:/usr/local/sbin:"
                + os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
            ),
        }
        log_fh = _UPDATE_LOG.open("a")
        subprocess.Popen(
            ["bash", "-c", cmd],
            start_new_session=True,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            close_fds=True,
            env=env,
        )
        # Popen dup'd the fd; close our handle so it doesn't leak.
        log_fh.close()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"не вдалось запустити оновлення: {exc}") from exc

    return {
        "status": "updating",
        "message": "Оновлення запущено — менеджер перезапуститься за ~30 секунд",
        "log": str(_UPDATE_LOG),
    }


_LOG_SOURCES = {
    "bhm": _INSTALL_DIR / "bhm.log",
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
    """
    import subprocess as _sp
    script = _INSTALL_DIR / "scripts" / "usb_probe.py"
    python = _INSTALL_DIR / ".venv" / "bin" / "python"
    if not script.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"{script} not found — run install --force to pull "
                "the latest scripts."
            ),
        )
    if not python.exists():
        raise HTTPException(
            status_code=500, detail=f"venv python not found at {python}",
        )
    try:
        proc = _sp.run(
            [str(python), str(script)],
            capture_output=True, text=True, timeout=15,
        )
    except _sp.TimeoutExpired:
        raise HTTPException(status_code=504, detail="usb probe timed out (>15s)")
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
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


_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.yaml"

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


def _render_uplink_block(enabled: bool, tenant: str, url: str = _DEFAULT_UPLINK_URL) -> str:
    return (
        "# Remote log uplink — managed by the dashboard. Toggle via\n"
        "# POST /system/uplink. Editing this block by hand is fine, but the\n"
        "# UI overwrites the entire block on each save, so any comments you\n"
        "# add INSIDE the block will be lost on the next toggle.\n"
        "uplink:\n"
        f"  enabled: {'true' if enabled else 'false'}\n"
        f"  url: \"{url}\"\n"
        f"  tenant: \"{tenant}\"\n"
        "  reconnect_delay: 2\n"
    )


def _replace_uplink_in_config(text: str, enabled: bool, tenant: str, url: str = _DEFAULT_UPLINK_URL) -> str:
    new_block = _render_uplink_block(enabled, tenant, url)
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
        "connected": bool(client and client.connected),
        "detected_tenant": detected_tenant,
    }


@router.post("/uplink")
async def set_uplink(payload: UplinkPayload, request: Request) -> dict:
    """Toggle uplink on/off. When turning ON: tenant is auto-detected
    from the most recent PWA Origin we've seen — if none, returns 400 so
    the dashboard can tell the operator to open the POS app first.

    Writes the new uplink block to config.yaml, then SIGTERM ourselves
    so the service manager (launchd / systemd / PM2 / runit) respawns
    the manager with the new config. Operators on bare `python main.py`
    will need to restart manually.
    """
    if payload.enabled:
        last_origin = getattr(request.app.state, "last_tenant_origin", "")
        tenant = _extract_tenant_from_origin(last_origin) if last_origin else None
        if not tenant:
            raise HTTPException(
                status_code=400,
                detail=(
                    "no tenant detected — open your POS app at its "
                    "subdomain (e.g. biergarten-lviv.barhandler.com) "
                    "and let it ping the manager once, then try again"
                ),
            )
    else:
        tenant = ""

    try:
        text = _CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"can't read {_CONFIG_PATH}: {exc}") from exc
    new_text = _replace_uplink_in_config(text, payload.enabled, tenant)
    try:
        _CONFIG_PATH.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"can't write {_CONFIG_PATH}: {exc}") from exc

    async def _suicide() -> None:
        await asyncio.sleep(2)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_suicide())
    return {
        "status": "saved",
        "message": "config.yaml оновлено, менеджер перезапуститься за 2 секунди",
        "uplink": {
            "enabled": payload.enabled,
            "tenant": tenant,
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
