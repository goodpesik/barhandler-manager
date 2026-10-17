"""PET-1087 — this workstation: who it is, what its devices did, what the
app asked to keep here.

The identifier is the install id the logs server already knows this machine
by (`install_id.txt`), so a device reported to the shop's server carries the
same name as the same machine's logs. The host name travels with it: an id
alone says nothing to the person reading a list of three tills.
"""

from __future__ import annotations

import logging
import platform
import socket
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from src.config import APP_DIR
from src.services import station_settings as station_settings_service
from src.version import installed_version

log = logging.getLogger(__name__)
router = APIRouter()


def station_id() -> str:
    """The stable identifier of this install. Born on first use and kept in
    APP_DIR, so it survives restarts and updates."""
    from src.services.log_uplink import get_or_create_install_id

    return get_or_create_install_id(APP_DIR / "install_id.txt")


def station_label() -> str:
    """What a person calls this machine. The host name is the only readable
    name we have without asking anybody to type one."""
    try:
        name = socket.gethostname() or ""
    except OSError:
        name = ""
    # macOS hands out `Maksym-MacBook.local`; the suffix adds nothing.
    return name.removesuffix(".local").strip()


def _journal(request: Request):
    return getattr(request.app.state, "work_journal", None)


def _settings(request: Request):
    return getattr(request.app.state, "station_settings", None)


@router.get("")
async def station(request: Request) -> dict:
    settings = _settings(request)
    journal = _journal(request)
    return {
        "installId": station_id(),
        "label": station_label(),
        "platform": platform.system(),
        "version": installed_version(),
        "shops": settings.shops() if settings is not None else [],
        "journalEntries": journal.count if journal is not None else 0,
        "journalLastSeq": journal.last_seq if journal is not None else 0,
    }


@router.get("/journal")
async def journal(
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict:
    """What the printers and the terminal of this station did, from row
    `after` on.

    `pageLastSeq` is THE CURSOR for the next call: the last row of this page.
    `lastSeq` is the station's highest number, which says how much is still
    queued — taking IT as the cursor would skip everything between the page
    and it (found by review), so both are named and both are read at once.
    """
    j = _journal(request)
    if j is None:
        # A build without a journal: an empty answer, not an error — the app
        # must not keep a troubled face over a manager that simply does not
        # keep one yet.
        return {"entries": [], "pageLastSeq": after, "lastSeq": 0}
    return j.page(after_seq=after, limit=limit)


@router.post("/settings")
async def save_settings(request: Request) -> dict:
    """The app leaves its device settings here — see `station_settings`."""
    s = _settings(request)
    if s is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "station_settings_unavailable",
                "message": "Ця версія Девайс менеджера не зберігає налаштування. Оновіть менеджер.",
            },
        )
    try:
        payload = await request.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "Некоректний запит."})
    try:
        # The raw value, not `str(...)`: a list would otherwise arrive as its
        # own repr and pass for a shop (found by review).
        return await _to_thread(s.save, payload.get("appid"), payload.get("items"))
    except station_settings_service.SettingsRefused as exc:
        log.warning("station settings refused: %s", exc)
        raise HTTPException(status_code=400, detail={"code": "settings_refused", "message": str(exc)})
    except OSError as exc:
        log.error("station settings were not saved: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"code": "settings_not_saved", "message": "Не вдалося зберегти налаштування на цьому комп'ютері."},
        )


@router.get("/settings")
async def read_settings(request: Request, appid: str = Query(default="")) -> dict:
    """The app takes them back when its own copy is empty."""
    s = _settings(request)
    blob: Optional[dict] = None if s is None else await _to_thread(s.load_shop, appid)
    if blob is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "no_station_settings", "message": "Немає збережених налаштувань."},
        )
    return blob


async def _to_thread(fn, *args):
    import asyncio

    return await asyncio.to_thread(fn, *args)
