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
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()

IS_WIN = os.name == "nt"

_INSTALL_DIR = Path.home() / ".barhandler-manager"
_UPDATE_LOG = _INSTALL_DIR / "update.log"


@router.get("/version")
async def get_version() -> dict:
    version_file = Path("VERSION")
    version = version_file.read_text().strip() if version_file.exists() else "unknown"
    return {"version": version}


def _build_update_argv() -> tuple[list[str], str]:
    """Return the (argv, human-readable description) for the update
    command on this OS. POSIX runs update.sh via bash; Windows runs
    update.ps1 via powershell. Each falls back to pulling the upstream
    installer directly when the on-disk helper is missing (e.g. a dev
    checkout that was never `install`-ed).

    The 2-second sleep gives the HTTP response time to reach the browser
    before the manager restarts out from under it.
    """
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
        argv = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-Command", inner,
        ]
        return argv, inner

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


@router.post("/update")
async def trigger_update() -> dict:
    """Spawn the platform updater fully detached from the manager so it
    survives the restart it triggers, and return immediately.

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
    argv, desc = _build_update_argv()

    try:
        _INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        # Append header so the operator can tell separate attempts apart
        # in update.log when they bang the button multiple times.
        with _UPDATE_LOG.open("a") as fh:
            fh.write(
                f"\n=== update triggered {_dt.datetime.now().isoformat()} "
                f"(pid={os.getpid()}) ===\n",
            )
            fh.write(f"cmd: {desc}\n")
            fh.flush()

        popen_kwargs: dict = dict(
            stdout=None,  # set below to the dup'd log fd
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
        if IS_WIN:
            popen_kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
            )
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

        log_fh = _UPDATE_LOG.open("a")
        popen_kwargs["stdout"] = log_fh
        try:
            subprocess.Popen(argv, **popen_kwargs)
        except OSError:
            # Breakaway can be refused by the job object — retry without
            # it rather than failing the whole update.
            if IS_WIN:
                popen_kwargs["creationflags"] = (
                    subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                )
                subprocess.Popen(argv, **popen_kwargs)
            else:
                raise
        finally:
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

    if payload.enabled:
        last_origin = getattr(state, "last_tenant_origin", "")
        tenant = _extract_tenant_from_origin(last_origin) if last_origin else None
        # If we already have a saved tenant in config (e.g. operator
        # clicked enable earlier), reuse it — origin tracking might be
        # stale right after a restart before the PWA pings the manager.
        if not tenant:
            tenant = cfg.get("uplink", {}).get("tenant") or None
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
        tenant = cfg.get("uplink", {}).get("tenant", "")

    # Persist to config.yaml so the next boot reflects this state.
    try:
        text = _CONFIG_PATH.read_text(encoding="utf-8")
        new_text = _replace_uplink_in_config(text, payload.enabled, tenant)
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
        install_id_path = Path(__file__).resolve().parent.parent.parent / "install_id.txt"
        install_id = get_or_create_install_id(install_id_path)
        version_path = Path(__file__).resolve().parent.parent.parent / "VERSION"
        version = version_path.read_text().strip() if version_path.exists() else "0.0.0"
        client = LogUplinkClient({
            "url": _DEFAULT_UPLINK_URL,
            "tenant": tenant,
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
