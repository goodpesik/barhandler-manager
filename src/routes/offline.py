"""PET-972 — offline mode: activation, state, switching off.

The online app (signed in as the shop's staff) takes a device token from the
server and hands it here; this machine then keeps the offline service running
for that shop. All calls need the manager's API key, like the device routes;
none of them returns a secret.

FC-798 — every call is for one product's till: `?product=` (or `product` in
the body), Petshandler when not said, so the Petshandler app and dashboard
calls work as before. FitStudioCRM's till runs beside it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import re

from fastapi import APIRouter, HTTPException, Request, Response

from src.services import offline_secrets, offline_state
from src.services.offline_service import DEFAULT_PRODUCT, PRODUCTS, OfflineService, safe_fiscal_path

log = logging.getLogger(__name__)
router = APIRouter()

#: What a call to these routes may weigh on the wire. Everything they take is
#: a small JSON object: an activation, a snapshot of device settings, a
#: register form. An uploaded key container is larger and has a cap of its own
#: (offline_state.FISCAL_KEY_MAX_REQUEST_BYTES).
MAX_BODY_BYTES = 1024 * 1024

#: The answer to a body over a route's cap. The request is refused while it is
#: still arriving, so the caller is told the size and not the content.
_TOO_LARGE = {"code": "body_too_large", "message": "Запит завеликий."}


class _BodyTooLarge(Exception):
    """The body went over the route's cap; the rest of it was never read."""


async def _capped_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """The request body, refused BEFORE the whole of it is held in memory.

    A cap checked after the body has been read is not a cap on memory: by then
    the whole body is in memory, and a copy of it again as soon as a field is
    taken out of it. A declared length over the cap is refused without reading
    anything at all, and a body that declares none (or declares less than it
    sends) is refused as soon as more than the cap has arrived — the rest is
    never pulled off the socket.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise _BodyTooLarge(f"{int(declared)} bytes declared, the cap is {limit}")
    chunks: list[bytes] = []
    read = 0
    async for chunk in request.stream():
        read += len(chunk)
        if read > limit:
            raise _BodyTooLarge(f"over the cap of {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _capped_json(request: Request, limit: int = MAX_BODY_BYTES) -> Optional[dict]:
    """The body as a JSON object, capped as above; None when it is not one."""
    try:
        payload = json.loads(await _capped_body(request, limit))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _refuse_too_large(request: Request, reason: str, detail: dict = _TOO_LARGE) -> HTTPException:
    """The refusal of an oversized body, with its size named in the log."""
    log.warning("offline: refused %s %s: %s", request.method, request.url.path, reason)
    return HTTPException(status_code=413, detail=detail)


def _product(request: Request, payload: Optional[dict] = None) -> str:
    """Which product's till the call is for; an unknown one is refused."""
    product = request.query_params.get("product") or (payload or {}).get("product") or DEFAULT_PRODUCT
    if product not in PRODUCTS:
        log.warning("offline: refused a call for unknown product %r", product)
        raise HTTPException(
            status_code=400,
            detail={"code": "offline_unknown_product", "message": "Офлайн-режим для цього продукту не підтримується."},
        )
    return product


def _service(request: Request, product: str = DEFAULT_PRODUCT) -> Optional[OfflineService]:
    services = getattr(request.app.state, "offline_services", None)
    if services is not None:
        return services.get(product)
    return getattr(request.app.state, "offline_service", None) if product == DEFAULT_PRODUCT else None


def _offered(request: Request, product: str = DEFAULT_PRODUCT) -> bool:
    offers = getattr(request.app.state, "offline_offers", None)
    if offers is not None:
        offer = offers.get(product)
    else:
        offer = getattr(request.app.state, "offline_offer", None) if product == DEFAULT_PRODUCT else None
    return bool(offer is not None and offer.offered())


@router.get("/status")
async def status(request: Request) -> dict:
    product = _product(request)
    svc = _service(request, product)
    result: dict = {
        # A build without the Node runtime cannot run offline mode at all.
        "available": svc is not None,
        # Whether the client app offers offline mode at all (off on
        # Petshandler's production for now). The dashboard shows the mode
        # only when it does, or when it is already on here.
        "offered": _offered(request, product),
        **offline_state.public_status(product),
    }
    if svc is not None:
        st = svc.state
        result["service"] = {
            "running": st.running,
            "version": st.version,
            "restarts": st.restarts,
            "lastError": st.last_error,
            # How the service stands beyond «running»: `idle`, `starting`,
            # `running` or `failed`. An app that has only `running` cannot
            # tell a till coming up from a till that will not come up, and
            # calls both broken.
            "startup": st.startup,
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
    product = _product(request)
    svc = _service(request, product)
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
    outcome = (
        " (it did not go through)"
        if ran.get("failed")
        else " (the server does not accept this device)"
        if ran.get("needsPairing")
        else ""
    )
    if product == "petshandler":
        # Petshandler keeps its original line: logs are searched by it.
        log.info("offline sync now: %s%s, %s still queued", ran.get("ran"), outcome, st.extra.get("queued"))
    else:
        log.info(
            "offline sync now (%s): %s%s, %s still queued",
            product, ran.get("ran"), outcome, st.extra.get("queued"),
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
    try:
        payload = await _capped_json(request)
    except _BodyTooLarge as e:
        raise _refuse_too_large(request, str(e))
    # The product is checked by the activation itself; only a known one picks
    # its supervisor here (an unknown one finds none and is refused below).
    wanted = str((payload or {}).get("product") or DEFAULT_PRODUCT)
    if _service(request, wanted) is None and wanted in PRODUCTS:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "offline_unavailable",
                "message": "Ця версія Девайс менеджера не підтримує офлайн-режим. Оновіть менеджер.",
            },
        )
    if payload is None:
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
    svc = _service(request, wanted)
    if svc is not None:
        # The supervisor starts the till for this activation now, and says it
        # is starting until it is up, so the app that has just switched the
        # mode on shows a start under way instead of a till that is not there.
        svc.activation_changed()
    return {"ok": True, **public}


@router.post("/deactivate")
async def deactivate(request: Request) -> dict:
    product = _product(request)
    svc = _service(request, product)
    appid = offline_state.active_appid(product)
    if not appid:
        return {"ok": True}
    # What the service says right now, not what it said a minute ago.
    health = await svc.refresh() if svc is not None else None
    queued = health.get("queued") if health else None
    if not isinstance(queued, int):
        # Not running (it never starts without its secrets) or not answering:
        # the queue is read from the local copy, so switching off does not
        # depend on the service it would switch off.
        queued = await asyncio.to_thread(offline_state.queued_on_disk, appid, product=product)
        log.info("offline switch-off: the service did not answer, queue on disk: %s", queued)
    pending = health.get("fiscalPending") if health else None
    if isinstance(pending, int) and not isinstance(pending, bool):
        fiscal_pending = pending
        fiscal_sessions = len(health.get("fiscalSessions") or [])
    else:
        # Not asked, or a service that does not say (an older build): the
        # counts the service keeps in the clear. A count is never taken for
        # «nothing owed» when it is not there — it comes back unjudged.
        on_disk = await asyncio.to_thread(offline_state.fiscal_owed_on_disk, appid, product)
        fiscal_pending, fiscal_sessions = on_disk.pending, on_disk.sessions
        if on_disk.unjudged:
            # The copy cannot say what it owes, and no restart will make it
            # say: the till would stay switched on for good. The switch-off
            # goes on, and the log carries what could not be read, so a shop
            # that comes back asking about a fiscal number can be answered.
            log.warning(
                "offline switch-off (%s shop %s): the fiscal state of the local copy "
                "could not be judged (%s); nothing names a document as owed, switching off goes on",
                product, appid, on_disk.unjudged,
            )
    try:
        offline_state.check_can_deactivate(
            queued if isinstance(queued, int) else None, fiscal_pending, fiscal_sessions,
        )
    except offline_state.ActivationError as e:
        log.warning("offline deactivation refused: %s", e)
        raise HTTPException(status_code=409, detail={"code": "offline_queue_not_empty", "message": str(e)})
    # Secrets first: without them the supervisor cannot start the service
    # again in between. If they cannot be removed, nothing else is touched.
    try:
        appid = await asyncio.to_thread(offline_state.forget, product)
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
            await asyncio.to_thread(offline_state.remove_data, appid, product)
    return {"ok": True}


@router.post("/local-settings")
async def save_local_settings(request: Request) -> dict:
    """PET-973 — the online app leaves its device settings for the offline build."""
    try:
        payload = await _capped_json(request)
    except _BodyTooLarge as e:
        raise _refuse_too_large(request, str(e))
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "Некоректний запит."})
    product = _product(request, payload)
    try:
        return await asyncio.to_thread(
            offline_state.save_local_settings, str(payload.get("appid") or ""), payload.get("items"), product,
        )
    except offline_state.ActivationError as e:
        # Not activated, or for another shop: the app simply has nothing to leave.
        raise HTTPException(status_code=409, detail={"code": "offline_not_active", "message": str(e)})


@router.post("/fiscal-key")
async def save_fiscal_key(request: Request) -> dict:
    """PET-1088 — the shop uploads the key container of an offline register.

    The offline service signs with a file on this machine, so the person picks
    it in the browser instead of typing a path. It is kept in the manager's own
    folder (it outlives the offline data, which goes when offline mode is
    switched off), and the answer carries the path the register settings keep.

    FC-844 — for the till named by `?product=` (or `product` in the body),
    Petshandler when not said: the file is kept in that product's own folder.

    The cap on a container is a cap on this request too, so a body far bigger
    than any container is refused while it is still arriving instead of being
    buffered, copied and only then measured.
    """
    try:
        payload = await _capped_json(request, offline_state.FISCAL_KEY_MAX_REQUEST_BYTES)
    except _BodyTooLarge as e:
        raise _refuse_too_large(
            request,
            str(e),
            {"code": "fiscal_key_too_large", "message": "Файл завеликий для ключа."},
        )
    if not isinstance(payload, dict):
        log.warning("offline fiscal key refused: the body is not an object")
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "Некоректний запит."})
    product = _product(request, payload)
    _require_registers(product)
    try:
        return await asyncio.to_thread(
            offline_state.save_fiscal_key,
            str(payload.get("appid") or ""),
            str(payload.get("fileName") or ""),
            str(payload.get("contentBase64") or ""),
            product,
        )
    except offline_state.ActivationError as e:
        log.warning("offline fiscal key refused: %s", e)
        raise HTTPException(status_code=400, detail={"code": "fiscal_key_refused", "message": str(e)})
    except OSError as e:
        log.error("offline fiscal key could not be kept: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"code": "fiscal_key_failed", "message": "Не вдалося зберегти файл ключа."},
        )


@router.get("/local-settings")
async def load_local_settings(request: Request) -> dict:
    """PET-973 — the offline build takes them when it starts."""
    body = await asyncio.to_thread(offline_state.load_local_settings, _product(request))
    if body is None:
        raise HTTPException(status_code=404, detail={"code": "no_local_settings", "message": "Немає збережених налаштувань."})
    return body


#: A register's id as it appears in a path: the service gave it out. It starts
#: with a letter or a digit, so `.` and `..` are not ids — an id of dots alone
#: turned `/registers/../grant` into the grant route and `/registers/..` into
#: the route that removes a register.
_REGISTER_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"

#: THE REGISTER ROUTES OF THE OFFLINE SERVICE (below /api/offline-fiscal),
#: NAMED ONE BY ONE WITH THE METHODS EACH ONE TAKES.
#:
#: An allow-list, not «any path that looks harmless»: what is behind these
#: routes sets up a cash register and hands documents to the tax service, and
#: a pass-through that forwards whatever it is given forwards the next route
#: the service grows — including one that was never meant to be reachable from
#: a browser over the internet. A route added to a service and not named here
#: is refused with its path in the log, which is what tells us to add it.
#:
#: The list covers both tills: Petshandler's registration and shift routes and
#: FitStudioCRM's `PATCH` on a register (FC-844 — the verb its admin's own api
#: client speaks). A method a till does not have simply never arrives.
_FISCAL_ROUTES: tuple[tuple[str, frozenset[str]], ...] = (
    # The registers of this computer and what state each one is in.
    ("", frozenset({"GET"})),
    # What the tax service has registered for the owner of a key.
    ("/lookup", frozenset({"POST"})),
    # Add a register; save or remove one by its id.
    ("/registers", frozenset({"POST"})),
    (rf"/registers/{_REGISTER_ID}", frozenset({"PUT", "PATCH", "DELETE"})),
    # The cashier's key: opened and kept here, then checked against the
    # tax service's own list.
    ("/cashier-key/prepare", frozenset({"POST"})),
    (rf"/registers/{_REGISTER_ID}/cashier-key/check", frozenset({"POST"})),
    # The right to work offline, and which machine the РРО is held for.
    (rf"/registers/{_REGISTER_ID}/grant", frozenset({"POST"})),
    (rf"/registers/{_REGISTER_ID}/device", frozenset({"POST"})),
    # Closing an offline session: hand its documents over, or drop a test
    # session the tax service refuses for good.
    (rf"/registers/{_REGISTER_ID}/return-online", frozenset({"POST"})),
    (rf"/registers/{_REGISTER_ID}/discard-test-session", frozenset({"POST"})),
    # The shift of a register (Petshandler).
    (rf"/registers/{_REGISTER_ID}/shift", frozenset({"POST"})),
    (rf"/registers/{_REGISTER_ID}/shift/close", frozenset({"POST"})),
    # Registering a РРО with the tax service (Petshandler).
    (rf"/registers/{_REGISTER_ID}/registration", frozenset({"GET", "PUT"})),
    (rf"/registers/{_REGISTER_ID}/registration/payer", frozenset({"POST"})),
    (rf"/registers/{_REGISTER_ID}/registration/submit", frozenset({"POST"})),
    (rf"/registers/{_REGISTER_ID}/registration/receipt", frozenset({"POST"})),
    (rf"/registers/{_REGISTER_ID}/registration/refresh", frozenset({"POST"})),
)

#: The same list compiled once; `fullmatch` everywhere, so a path with
#: anything appended to it is a different path.
_FISCAL_ALLOWED: tuple[tuple["re.Pattern[str]", frozenset[str]], ...] = tuple(
    (re.compile(pattern), methods) for pattern, methods in _FISCAL_ROUTES
)


def _fiscal_route_allowed(method: str, path: str) -> bool:
    """Whether this method and path are one of the register routes above.

    The same guard that stands where the path is pasted into the service's URL
    runs here first, so a path that would have to be normalised is refused
    with a 400 instead of reaching the pass-through at all.
    """
    try:
        safe_fiscal_path(path)
    except ValueError:
        return False
    return any(
        method in methods and pattern.fullmatch(path)
        for pattern, methods in _FISCAL_ALLOWED
    )


def _require_registers(product: str) -> None:
    """Refuse a product whose offline till keeps no registers of its own.

    Such a till fiscalises through its server after the sync (PRODUCTS[…]
    .fiscal), so there is nothing here to pass a register route on to.
    """
    if not PRODUCTS[product].fiscal:
        log.warning("offline fiscal: refused, the %s till keeps no registers of its own", product)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "offline_no_registers",
                "message": "Офлайн-каса цього продукту не має власних РРО на цьому компʼютері.",
            },
        )


@router.api_route("/fiscal{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def fiscal(path: str, request: Request) -> Response:
    """The offline registers of this computer, set up from the online app.

    The registers belong to the offline service here (their key is on this
    disk), and the service lets no other web page in. The online app reaches
    them through this route, with the manager's key, so a shop sets its
    register up while it still has internet, before it ever has to go offline.
    The bodies are passed through and never logged: they carry the key's
    password.

    FC-844 — for the till named by `?product=`, Petshandler when not said:
    the FitStudioCRM card reaches its own service on its own port.
    """
    product = _product(request)
    _require_registers(product)
    svc = _service(request, product)
    if svc is None:
        log.warning("offline fiscal: refused, this build runs no %s offline till", product)
        raise HTTPException(status_code=503, detail={"code": "offline_unavailable", "message": "Ця збірка не підтримує офлайн-режим."})
    if not offline_state.active_appid(product):
        log.warning("offline fiscal: refused, %s offline mode is not on here", product)
        raise HTTPException(status_code=409, detail={"code": "offline_not_active", "message": "Офлайн-режим на цьому комп'ютері не увімкнено."})
    if not _fiscal_route_allowed(request.method, path):
        log.warning("offline fiscal: refused %s %r — not a register route", request.method, path)
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "Некоректний запит."})
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await _capped_body(request)
        except _BodyTooLarge as e:
            raise _refuse_too_large(request, str(e))
    else:
        body = None
    try:
        status, answer = await svc.fiscal(request.method, path, body)
    except Exception as e:  # noqa: BLE001 — refused, timeout: the service is not there
        log.warning(
            "offline fiscal %s %s (%s): the service did not answer: %s",
            request.method, path, product, e,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "offline_service_down", "message": "Офлайн-сервіс не відповідає. Спробуйте за хвилину."},
        )
    log.info("offline fiscal %s %s (%s): %s", request.method, path or "/", product, status)
    return Response(content=answer, status_code=status, media_type="application/json")
