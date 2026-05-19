"""Device discovery + registration routes.

Workflow (frontend-driven):

  1. Settings UI presses "Виявити пристрої"
     → POST /devices/discover  -> [{ id, transport, label, ... }]

  2. User picks one card, assigns role + nickname, submits
     → POST /devices/register  body: { id, kind, nickname, paper_width }
     The chosen id is what the frontend stores in app settings.

  3. Later, when printing, the frontend passes ?printer_id=<id> (or
     omits it to use the registered "receipt" default).

  4. To clear:
     → DELETE /devices/{id}
"""

from fastapi import APIRouter, HTTPException, Request

from src.devices.registry import UnknownPrinter
from src.models.printer import (
    PrinterDescriptor,
    PrinterRegistration,
    RegistrationRequest,
)

router = APIRouter()


def _registry(request: Request):
    return request.app.state.registry


@router.post("/discover")
async def discover(request: Request) -> dict:
    """Scan every supported transport and return the candidates."""
    descriptors: list[PrinterDescriptor] = _registry(request).discover()
    return {"printers": [d.model_dump() for d in descriptors]}


@router.get("")
async def list_registered(request: Request) -> dict:
    regs: list[PrinterRegistration] = _registry(request).all_registrations()
    return {"printers": [r.model_dump() for r in regs]}


@router.get("/scan")
async def scan(request: Request) -> dict:
    """Alias for /devices/discover (kept for backwards compat)."""
    return await discover(request)


@router.post("/register")
async def register(payload: RegistrationRequest, request: Request) -> dict:
    try:
        reg = _registry(request).register(payload)
    except UnknownPrinter as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"printer": reg.model_dump()}


@router.delete("/{printer_id}")
async def unregister(printer_id: str, request: Request) -> dict:
    try:
        _registry(request).unregister(printer_id)
    except UnknownPrinter as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "removed", "id": printer_id}
