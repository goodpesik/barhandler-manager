"""System management endpoints — update + version info.

POST /system/update  — spawns update.sh as a detached process and returns
                       immediately. The manager will restart itself within
                       a few seconds as the installer replaces the code.
GET  /system/version — returns the running version so the dashboard JS
                       can compare it with the latest GitHub release without
                       needing a dedicated GitHub API proxy.
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException

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
