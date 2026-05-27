"""System management endpoints — update + version info.

POST /system/update  — spawns update.sh as a detached process and returns
                       immediately. The manager will restart itself within
                       a few seconds as the installer replaces the code.
GET  /system/version — returns the running version so the dashboard JS
                       can compare it with the latest GitHub release without
                       needing a dedicated GitHub API proxy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/version")
async def get_version() -> dict:
    version_file = Path("VERSION")
    version = version_file.read_text().strip() if version_file.exists() else "unknown"
    return {"version": version}


@router.post("/update")
async def trigger_update() -> dict:
    """Spawn update.sh in a new session (detached from the manager process)
    so it survives the manager restart it triggers. A 2-second sleep gives
    the HTTP response time to reach the browser before the process dies."""
    script = Path.home() / ".barhandler-manager" / "update.sh"
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
        subprocess.Popen(
            ["bash", "-c", cmd],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"не вдалось запустити оновлення: {exc}") from exc

    return {"status": "updating", "message": "Оновлення запущено — менеджер перезапуститься за ~30 секунд"}
