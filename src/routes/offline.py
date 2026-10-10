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

from src.services import offline_secrets, offline_state
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
            # PET-1055 — where the offline till is served, so neither the
            # dashboard nor Petshandler has to know the number by heart.
            "port": svc.port,
            **st.extra,
        }
    return result


@router.post("/sync")
async def sync_now(request: Request) -> dict:
    """
    PET-1057 — «Синхронізувати» in Petshandler: send what the till did
    offline now, instead of waiting for the service's own schedule.

    Answers with what the sync did and the state after it, so the card can
    show the new «as of» and how much is still queued.
    """
    svc = _service(request)
    if svc is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "offline_unavailable",
                "message": "Ця версія Девайс менеджера не підтримує офлайн-режим. Оновіть менеджер.",
            },
        )
    if not svc.state.running:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "offline_not_running",
                "message": "Офлайн-сервіс не запущено на цьому комп'ютері.",
            },
        )
    ran = await svc.sync_now()
    if ran is None:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "offline_sync_failed",
                "message": "Не вдалося синхронізувати. Спробуйте ще раз.",
            },
        )
    st = svc.state
    log.info(
        "offline sync now: %s%s, %s still queued",
        ran.get("ran"),
        " (it did not go through)"
        if ran.get("failed")
        else " (the server does not accept this device)"
        if ran.get("needsPairing")
        else "",
        st.extra.get("queued"),
    )
    return {
        "ran": ran.get("ran"),
        # PET-1057 — «it did nothing», «it did not go through» and «this
        # device is not accepted» must not reach the shop as one answer.
        "failed": bool(ran.get("failed")),
        # It may have become so during this very sync, after the check that
        # let the request in.
        "needsPairing": bool(ran.get("needsPairing") or st.extra.get("needsPairing")),
        "dataAsOf": st.extra.get("dataAsOf", ran.get("dataAsOf")),
        "queued": st.extra.get("queued", ran.get("queued")),
    }


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
    except offline_secrets.SecretsUnreadable as e:
        # The key of the local copy may be in that file: nothing was changed.
        log.error("offline activation stopped, the secrets file is unreadable: %s", e)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "secrets_unreadable",
                "message": "Не вдалося прочитати збережений ключ офлайн-режиму. Нічого не змінено — спробуйте ще раз.",
            },
        )
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
    appid = offline_state.active_appid()
    if not appid:
        return {"ok": True}
    # What the service says right now, not what it said a minute ago.
    health = await svc.refresh() if svc is not None else None
    queued = health.get("queued") if health else None
    if not isinstance(queued, int):
        # Not running (it never starts without its secrets) or not answering:
        # the queue is read from the local copy, so switching off does not
        # depend on the service it would switch off.
        queued = await asyncio.to_thread(offline_state.queued_on_disk, appid)
        log.info("offline switch-off: the service did not answer, queue on disk: %s", queued)
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
