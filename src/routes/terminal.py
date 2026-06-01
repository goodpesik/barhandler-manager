"""POS terminal HTTP API.

Mirrors the printer route surface — discover / register / list /
unregister + the per-terminal action endpoints (charge, cancel,
status, list-merchants, ping). The route layer is bank-agnostic;
adapter selection happens inside the registry.

The terminal registry instance is attached to `app.state.terminal_registry`
during app startup (lifespan in `src/server.py`). When that hookup
isn't present yet we fall back to a lazily-built default — keeps the
old `/charge` stub functional during migration.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src.devices.terminal_registry import TerminalRegistry, UnknownTerminal
from src.models.terminal import (
    ChargeRequest,
    MerchantBinding,
    MerchantNicknameUpdate,
    TerminalRegistrationRequest,
)
from src.services.terminals.base import TerminalUnavailable

router = APIRouter()


def _registry(request: Request) -> TerminalRegistry:
    """Pull the singleton registry off app state, building one on
    demand so we never blow up on a fresh install."""
    registry = getattr(request.app.state, "terminal_registry", None)
    if registry is None:
        registry = TerminalRegistry()
        registry.load()
        request.app.state.terminal_registry = registry
    return registry


def _resolve(request: Request, terminal_id: Optional[str]):
    """Pick the requested terminal (or the only registered one if the
    caller didn't specify) and instantiate its adapter."""
    registry = _registry(request)
    if terminal_id is None:
        reg = registry.first()
        if reg is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "no_terminal",
                    "message": "no terminal registered — POST /terminal/register first",
                },
            )
        terminal_id = reg.descriptor.id
    try:
        return registry.adapter_for(terminal_id), terminal_id
    except UnknownTerminal as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_terminal", "message": str(exc)},
        )


def _surface_terminal_error(exc: TerminalUnavailable) -> HTTPException:
    """Map a TerminalUnavailable onto the same `{detail: {code, message}}`
    shape the printer routes use, so the frontend's existing 503
    error-handler picks both up uniformly."""
    return HTTPException(
        status_code=503,
        detail={"code": getattr(exc, "code", "unavailable"), "message": str(exc)},
    )


# ---------------------------------------------------------------------------
# Discovery + registration
# ---------------------------------------------------------------------------


@router.post("/discover")
async def discover(request: Request) -> dict:
    """LAN-scan + probe stub. Real LAN-scan extension lands in commit 3.
    For now we return whatever's cached from the last call (so unit
    tests that monkey-patch the discovery hook still work) plus the
    Bluetooth-style platform warnings the printer endpoint uses.
    """
    import asyncio

    from src.devices.scan import discover_network_terminals

    registry = _registry(request)
    descriptors = await asyncio.to_thread(discover_network_terminals)
    registry.remember_descriptors(descriptors)
    return {"terminals": [d.model_dump() for d in descriptors]}


@router.get("")
async def list_registered(request: Request) -> dict:
    return {
        "terminals": [r.model_dump() for r in _registry(request).all_registrations()],
    }


@router.post("/register")
async def register(payload: TerminalRegistrationRequest, request: Request) -> dict:
    try:
        reg = _registry(request).register(payload)
    except UnknownTerminal as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_terminal", "message": str(exc)},
        )
    return {"terminal": reg.model_dump()}


@router.delete("/{terminal_id}")
async def unregister(terminal_id: str, request: Request) -> dict:
    try:
        _registry(request).unregister(terminal_id)
    except UnknownTerminal as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_terminal", "message": str(exc)},
        )
    return {"status": "unregistered", "terminal_id": terminal_id}


# ---------------------------------------------------------------------------
# Per-terminal operations
# ---------------------------------------------------------------------------


@router.post("/{terminal_id}/ping")
async def ping(terminal_id: str, request: Request) -> dict:
    adapter, _ = _resolve(request, terminal_id)
    try:
        ok = await adapter.ping()
    except TerminalUnavailable as exc:
        raise _surface_terminal_error(exc)
    return {"ok": ok, "terminal_id": terminal_id}


@router.get("/{terminal_id}/info")
async def info(terminal_id: str, request: Request) -> dict:
    adapter, _ = _resolve(request, terminal_id)
    try:
        return {"terminal_id": terminal_id, "info": await adapter.get_info()}
    except TerminalUnavailable as exc:
        raise _surface_terminal_error(exc)


@router.get("/{terminal_id}/merchants")
async def list_merchants(terminal_id: str, request: Request) -> dict:
    """Live merchant roster from the terminal, merged with operator-
    set nicknames the registry has remembered. New bank-side merchants
    show up here on the next reload; removed ones disappear; nicknames
    survive both."""
    adapter, _ = _resolve(request, terminal_id)
    try:
        merchants = await adapter.list_merchants()
    except TerminalUnavailable as exc:
        raise _surface_terminal_error(exc)
    fresh = [
        MerchantBinding(
            merchant_id=m.merchant_id,
            terminal_id=m.terminal_id,
            merchant_name=m.merchant_name,
        )
        for m in merchants
    ]
    registry = _registry(request)
    try:
        merged = registry.merge_merchant_list(terminal_id, fresh)
    except UnknownTerminal:
        # Caller asked for merchants on an un-registered terminal —
        # still return the live list (useful during the discover/
        # register flow, when the UI peeks before persisting).
        merged = fresh
    return {
        "terminal_id": terminal_id,
        "merchants": [m.model_dump() for m in merged],
    }


@router.put("/{terminal_id}/merchants")
async def update_merchants(
    terminal_id: str, payload: MerchantNicknameUpdate, request: Request,
) -> dict:
    """Bulk-update merchant nicknames. The Settings UI sends the full
    list (operator may rename multiple at once); we replace what's
    stored. Nicknames are what the cashier sees in the merchant
    select at payment time."""
    try:
        reg = _registry(request).update_merchants(terminal_id, payload.merchants)
    except UnknownTerminal as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_terminal", "message": str(exc)},
        )
    return {
        "terminal_id": terminal_id,
        "merchants": [m.model_dump() for m in reg.merchants],
    }


@router.post("/charge")
async def charge(
    payload: ChargeRequest,
    request: Request,
    terminal_id: Optional[str] = None,
) -> dict:
    """Single-call payment: send Purchase, wait for the terminal to
    finish chip/PIN, return the unified AcquirerResult. The frontend's
    NgRx effect awaits this and feeds the result into the fiscal
    receipt flow."""
    import logging
    log = logging.getLogger("src.routes.terminal")
    adapter, used_id = _resolve(request, terminal_id)
    log.info(
        "[charge] terminal=%s payload=%s",
        used_id,
        payload.model_dump_json(),
    )
    try:
        result = await adapter.charge(payload)
    except TerminalUnavailable as exc:
        log.warning("[charge] terminal=%s TerminalUnavailable code=%s msg=%s", used_id, getattr(exc, "code", None), exc)
        raise _surface_terminal_error(exc)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — surface root cause instead of bare 500
        log.exception("[charge] terminal=%s unhandled error: %s", used_id, exc)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "internal_error",
                "message": f"{type(exc).__name__}: {exc}",
            },
        )
    log.info("[charge] terminal=%s result=%s", used_id, result.model_dump_json())
    return {"terminal_id": used_id, "result": result.model_dump()}


@router.post("/{terminal_id}/cancel")
async def cancel(terminal_id: str, request: Request) -> dict:
    """Operator hit Cancel mid-transaction. Only effective in S02/S03/S08
    per SSI doc §5.4.2 — otherwise the terminal ignores it. Adapter
    swallows the error in that case; we always return 200."""
    adapter, _ = _resolve(request, terminal_id)
    await adapter.cancel()
    return {"status": "cancel_requested", "terminal_id": terminal_id}


@router.get("/{terminal_id}/last-result")
async def last_result(
    terminal_id: str,
    request: Request,
    transaction_uid: Optional[str] = None,
) -> dict:
    """Recovery hook — fetch the most recent (or specific-uid) result
    after a network drop mid-charge. Frontend uses this to confirm a
    transaction it thinks succeeded but didn't get the final result
    for."""
    adapter, _ = _resolve(request, terminal_id)
    try:
        result = await adapter.get_last_result(transaction_uid=transaction_uid)
    except TerminalUnavailable as exc:
        raise _surface_terminal_error(exc)
    return {"terminal_id": terminal_id, "result": result.model_dump()}
