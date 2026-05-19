"""Print + drawer routes — pick the target printer by `printer_id`.

If `printer_id` is omitted the manager falls back to the first registered
printer of the appropriate role:
  - /print/receipt, /print/fiscal, /print/text  → role 'receipt'
  - /print/kitchen                              → role 'kitchen'

Connections are opened lazily by PrinterRegistry and reused across prints.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.devices.printer import PrinterUnavailable
from src.devices.registry import UnknownPrinter
from src.models.fiscal_receipt import FiscalReceipt
from src.models.printer import PrinterKind
from src.models.receipt import ReceiptPayload
from src.services.fiscal_receipt import render_fiscal_receipt
from src.services.receipt import render_receipt

router = APIRouter()


async def _resolve_printer(request: Request, printer_id: Optional[str], kind: PrinterKind):
    registry = request.app.state.registry
    if printer_id is None:
        reg = registry.for_kind(kind)
        if reg is None:
            raise HTTPException(
                status_code=503,
                detail=f"no '{kind.value}' printer registered — POST /devices/register first",
            )
        printer_id = reg.descriptor.id
    try:
        reg = registry.get_registration(printer_id)
    except UnknownPrinter:
        raise HTTPException(status_code=404, detail=f"unknown printer_id: {printer_id}")
    try:
        device = await registry.get_device(printer_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"printer connect failed: {exc}")
    if not device.is_connected():
        raise HTTPException(status_code=503, detail="printer_unavailable")
    return reg, device


def _maybe_open_drawer(esc, registration, open_drawer: bool) -> None:
    if open_drawer and registration.drawer_pin is not None:
        try:
            esc.cashdraw(int(registration.drawer_pin))
        except Exception:
            pass  # drawer wiring is optional


@router.post("/receipt")
async def print_receipt(
    payload: ReceiptPayload,
    request: Request,
    printer_id: Optional[str] = Query(default=None),
):
    reg, device = await _resolve_printer(request, printer_id, PrinterKind.receipt)
    chars = reg.chars_per_line

    async def _job(esc):
        render_receipt(esc, payload, chars_per_line=chars)
        _maybe_open_drawer(esc, reg, payload.open_drawer)

    try:
        await device.enqueue(_job)
    except PrinterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "printed", "printer_id": reg.descriptor.id}


@router.post("/fiscal")
async def print_fiscal(
    payload: FiscalReceipt,
    request: Request,
    printer_id: Optional[str] = Query(default=None),
):
    reg, device = await _resolve_printer(request, printer_id, PrinterKind.receipt)
    chars = reg.chars_per_line

    async def _job(esc):
        render_fiscal_receipt(esc, payload, chars_per_line=chars)
        _maybe_open_drawer(esc, reg, payload.open_drawer)

    try:
        await device.enqueue(_job)
    except PrinterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "printed", "printer_id": reg.descriptor.id}


class RawTextPayload(BaseModel):
    text: str
    open_drawer: bool = False


@router.post("/text")
async def print_text(
    payload: RawTextPayload,
    request: Request,
    printer_id: Optional[str] = Query(default=None),
):
    """Print pre-rendered text verbatim (Checkbox /text endpoint output)."""
    reg, device = await _resolve_printer(request, printer_id, PrinterKind.receipt)

    async def _job(esc):
        esc.set(align="left", bold=False, double_height=False, double_width=False)
        esc.text("\n\n")  # leading padding so the printer's cutter doesn't shave the header
        esc.text(payload.text.rstrip() + "\n\n\n")
        esc.cut()
        _maybe_open_drawer(esc, reg, payload.open_drawer)

    try:
        await device.enqueue(_job)
    except PrinterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "printed", "printer_id": reg.descriptor.id}


class FormattedLine(BaseModel):
    text: str = ""
    align: str = "left"  # "left" | "center" | "right"
    bold: bool = False
    double_height: bool = False
    double_width: bool = False


class LinesPayload(BaseModel):
    """Structured-line variant of /print/text — caller hands us per-line
    formatting (bold / align / double_*) instead of pre-formatted ASCII.

    Useful when the frontend wants to emphasise a header (#0003, СУМА)
    or centre a block without manually padding spaces."""

    lines: list[FormattedLine] = Field(default_factory=list)
    open_drawer: bool = False


@router.post("/lines")
async def print_lines(
    payload: LinesPayload,
    request: Request,
    printer_id: Optional[str] = Query(default=None),
):
    reg, device = await _resolve_printer(request, printer_id, PrinterKind.receipt)

    async def _job(esc):
        esc.set(align="left", bold=False, double_height=False, double_width=False)
        esc.text("\n\n")  # leading padding — same rationale as /text
        for line in payload.lines:
            esc.set(
                align=line.align,
                bold=line.bold,
                double_height=line.double_height,
                double_width=line.double_width,
            )
            esc.text((line.text or "") + "\n")
        esc.set(align="left", bold=False, double_height=False, double_width=False)
        esc.text("\n\n\n")
        esc.cut()
        _maybe_open_drawer(esc, reg, payload.open_drawer)

    try:
        await device.enqueue(_job)
    except PrinterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "printed", "printer_id": reg.descriptor.id}


class KitchenItem(BaseModel):
    name: str
    qty: int = 1
    note: Optional[str] = None


class KitchenTicket(BaseModel):
    order_number: str
    table: Optional[str] = None
    guest: Optional[str] = None
    items: list[KitchenItem]
    comment: Optional[str] = None


@router.post("/kitchen")
async def print_kitchen(
    payload: KitchenTicket,
    request: Request,
    printer_id: Optional[str] = Query(default=None),
):
    """Kitchen ticket — large item names, no totals, no fiscal block.
    Defaults to the first registered printer with kind=kitchen."""
    reg, device = await _resolve_printer(request, printer_id, PrinterKind.kitchen)
    chars = reg.chars_per_line

    async def _job(esc):
        esc.set(align="center", bold=True, double_height=True, double_width=False)
        esc.text(f"#{payload.order_number}\n")
        esc.set(align="left", bold=False, double_height=False)
        if payload.table:
            esc.text(f"Стіл: {payload.table}\n")
        if payload.guest:
            esc.text(f"Гість: {payload.guest}\n")
        esc.text("-" * chars + "\n")
        for item in payload.items:
            esc.set(bold=True, double_height=True)
            esc.text(f"{item.qty}x {item.name}\n")
            esc.set(bold=False, double_height=False)
            if item.note:
                esc.text(f"  {item.note}\n")
        if payload.comment:
            esc.text("-" * chars + "\n")
            esc.text(payload.comment + "\n")
        esc.text("\n\n")
        esc.cut()

    try:
        await device.enqueue(_job)
    except PrinterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "printed", "printer_id": reg.descriptor.id}
