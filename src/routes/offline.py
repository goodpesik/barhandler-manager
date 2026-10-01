"""PET-972 — offline mode of Petshandler: activation, state, switching off.

Petshandler (the online app, signed in as the shop's staff) takes a device
token from the server and hands it here; this machine then keeps the offline
service running for that shop. All three calls need the manager's API key,
like the device routes; none of them returns a secret.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src.services import offline_state
from src.services.offline_service import OfflineService

log = logging.getLogger(__name__)
router = APIRouter()


def _service(request: Request) -> Optional[OfflineService]:
    return getattr(request.app.state, "offline_service", None)


@router.get("/status")
async def status(request: Request) -> dict:
    svc = _service(request)
    result: dict = {
        # A build without the Node runtime cannot run offline mode at all.
        "available": svc is not None,
        **offline_state.public_status(),
    }
    if svc is not None:
        st = svc.state
        result["service"] = {
            "running": st.running,
            "version": st.version,
            "restarts": st.restarts,
            "lastError": st.last_error,
            **st.extra,
        }
    return result


@router.post("/activate")
async def activate(request: Request) -> dict:
    if _service(request) is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "offline_unavailable",
                "message": "Ця версія Девайс менеджера не підтримує офлайн-режим. Оновіть менеджер.",
            },
        )
    try:
        payload = await request.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "Некоректний запит."})
    try:
        public = await asyncio.to_thread(offline_state.activate, payload)
    except offline_state.ActivationError as e:
        log.warning("offline activation refused: %s", e)
        raise HTTPException(status_code=400, detail={"code": "activation_refused", "message": str(e)})
    except OSError as e:
        log.error("offline activation failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"code": "activation_failed", "message": "Не вдалося зберегти дані активації."},
        )
    return {"ok": True, **public}


@router.post("/deactivate")
async def deactivate(request: Request) -> dict:
    svc = _service(request)
    if not offline_state.active_appid():
        return {"ok": True}
    # What the service says right now, not what it said a minute ago.
    health = await svc.refresh() if svc is not None else None
    queued = health.get("queued") if health else None
    try:
        offline_state.check_can_deactivate(queued if isinstance(queued, int) else None)
    except offline_state.ActivationError as e:
        log.warning("offline deactivation refused: %s", e)
        raise HTTPException(status_code=409, detail={"code": "offline_queue_not_empty", "message": str(e)})
    # Secrets first: without them the supervisor cannot start the service
    # again in between. If they cannot be removed, nothing else is touched.
    try:
        appid = await asyncio.to_thread(offline_state.forget)
    except OSError as e:
        log.error("offline switch-off: secrets could not be removed: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"code": "deactivation_failed", "message": "Не вдалося вимкнути офлайн-режим. Спробуйте ще раз."},
        )
    try:
        if svc is not None:
            await svc.stop_service()
    except Exception as e:  # noqa: BLE001 — the data goes regardless, see below
        log.error("offline switch-off: the service did not stop cleanly: %s", e)
    finally:
        # The data key is gone with the secrets: the local copy can no longer
        # be read, so it is removed even if the service misbehaved.
        if appid:
            await asyncio.to_thread(offline_state.remove_data, appid)
    return {"ok": True}


@router.post("/local-settings")
async def save_local_settings(request: Request) -> dict:
    """PET-973 — the online app leaves its device settings for the offline build."""
    try:
        payload = await request.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "Некоректний запит."})
    try:
        return await asyncio.to_thread(
            offline_state.save_local_settings, str(payload.get("appid") or ""), payload.get("items"),
        )
    except offline_state.ActivationError as e:
        # Not activated, or for another shop: the app simply has nothing to leave.
        raise HTTPException(status_code=409, detail={"code": "offline_not_active", "message": str(e)})


@router.get("/local-settings")
async def load_local_settings() -> dict:
    """PET-973 — the offline build takes them when it starts."""
    body = await asyncio.to_thread(offline_state.load_local_settings)
    if body is None:
        raise HTTPException(status_code=404, detail={"code": "no_local_settings", "message": "Немає збережених налаштувань."})
    return body
