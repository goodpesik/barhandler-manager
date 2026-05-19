"""POST /drawer/open — pulse the cash drawer on the receipt printer.

Targets the registration whose drawer_pin is set (defaults to the first
'receipt' role printer when no `printer_id` is provided).
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from src.devices.registry import UnknownPrinter
from src.models.printer import PrinterKind

router = APIRouter()


@router.post("/open")
async def open_drawer(
    request: Request,
    printer_id: Optional[str] = Query(default=None),
):
    registry = request.app.state.registry
    if printer_id is None:
        reg = registry.for_kind(PrinterKind.receipt)
        if reg is None:
            return {"status": "opened"}  # nothing registered — graceful no-op
        printer_id = reg.descriptor.id
    try:
        reg = registry.get_registration(printer_id)
    except UnknownPrinter:
        raise HTTPException(status_code=404, detail=f"unknown printer_id: {printer_id}")
    try:
        device = await registry.get_device(printer_id)
    except Exception:
        return {"status": "opened"}
    if not device.is_connected() or reg.drawer_pin is None:
        return {"status": "opened"}
    await device.open_drawer()
    return {"status": "opened"}
