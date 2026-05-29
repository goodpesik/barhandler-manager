"""Print + drawer routes — pick the target printer by `printer_id`.

If `printer_id` is omitted the manager falls back to the first registered
printer of the appropriate role:
  - /print/receipt, /print/fiscal, /print/text  → role 'receipt'
  - /print/kitchen                              → role 'kitchen'
  - /print/label                                → role 'label'

Connections are opened lazily by PrinterRegistry and reused across prints.
"""

import base64
import io
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from PIL import Image
from pydantic import BaseModel, Field

from src.devices.printer import PrinterUnavailable
from src.devices.registry import UnknownPrinter
from src.models.fiscal_receipt import FiscalReceipt
from src.models.printer import PrinterKind
from src.models.receipt import ReceiptPayload
from src.services.bitmap_render import dots_for, image_to_gs_v_0
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
        # Surface the structured code so the frontend can switch on it
        # ("out_of_paper" → "Закінчився папір", "cover_open" → "Закрийте кришку").
        raise HTTPException(
            status_code=503,
            detail={"code": getattr(exc, "code", "unavailable"), "message": str(exc)},
        )
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
        # Surface the structured code so the frontend can switch on it
        # ("out_of_paper" → "Закінчився папір", "cover_open" → "Закрийте кришку").
        raise HTTPException(
            status_code=503,
            detail={"code": getattr(exc, "code", "unavailable"), "message": str(exc)},
        )
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
        # Surface the structured code so the frontend can switch on it
        # ("out_of_paper" → "Закінчився папір", "cover_open" → "Закрийте кришку").
        raise HTTPException(
            status_code=503,
            detail={"code": getattr(exc, "code", "unavailable"), "message": str(exc)},
        )
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
        # Surface the structured code so the frontend can switch on it
        # ("out_of_paper" → "Закінчився папір", "cover_open" → "Закрийте кришку").
        raise HTTPException(
            status_code=503,
            detail={"code": getattr(exc, "code", "unavailable"), "message": str(exc)},
        )
    return {"status": "printed", "printer_id": reg.descriptor.id}


class LabelPayload(BaseModel):
    image_base64: str


@router.post("/label")
async def print_label(
    payload: LabelPayload,
    request: Request,
    printer_id: Optional[str] = Query(default=None),
):
    """Print a pre-rendered label image (base64 PNG) on the label printer.

    The image is resized to exactly the printer's dot width, converted to
    1-bit, and sent via GS v 0. No paper cut — label printers use tear-off
    or continuous stock; cutting would jam the mechanism.
    """
    reg, device = await _resolve_printer(request, printer_id, PrinterKind.label)
    dot_width = dots_for(reg.paper_width)

    try:
        raw = base64.b64decode(payload.image_base64)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid image_base64: {exc}")

    # Scale to exactly dot_width pixels wide, preserving aspect ratio.
    orig_w, orig_h = img.size
    if orig_w != dot_width:
        new_h = max(1, int(orig_h * dot_width / orig_w))
        img = img.resize((dot_width, new_h), Image.LANCZOS)

    img = img.convert("1")
    raster = image_to_gs_v_0(img)

    async def _job(esc):
        esc._raw(raster)

    try:
        await device.enqueue(_job)
    except PrinterUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": getattr(exc, "code", "unavailable"), "message": str(exc)},
        )
    return {"status": "printed", "printer_id": reg.descriptor.id}


class KitchenItem(BaseModel):
    """One physical item that needs cooking. Each row prints as its own
    self-contained block with position number, name+measurement, table
    and guest — so the kitchen can detach individual items if multiple
    cooks are working the same order."""

    name: str
    qty: int = 1
    note: Optional[str] = None
    position: Optional[int] = None
    measurement: Optional[str] = None
    table: Optional[str] = None
    guest: Optional[str] = None


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
    """Kitchen ticket — one self-contained block per physical item so
    the kitchen can clip / tear off rows. Order number is the prominent
    header; the trailing padding is generous (10 blank lines) because
    short tickets curl up on the rail otherwise.

    Defaults to the first registered printer with kind=kitchen."""
    reg, device = await _resolve_printer(request, printer_id, PrinterKind.kitchen)
    chars = reg.chars_per_line
    sep = "=" * chars

    async def _job(esc):
        # --- order header (big & bold) ---
        esc.set(align="left", bold=False, double_height=False, double_width=False)
        esc.text(sep + "\n")
        esc.set(align="center", bold=True, double_height=True, double_width=False)
        esc.text(f"#{payload.order_number}\n")
        esc.set(align="left", bold=False, double_height=False, double_width=False)
        esc.text(sep + "\n")

        # --- one block per item ---
        for index, item in enumerate(payload.items, start=1):
            position = item.position if item.position is not None else index
            esc.set(align="center", bold=False, double_height=True, double_width=False)
            esc.text(f"{position}\n")
            # Item name + measurement big bold left so the cook reads it
            # from across the line.
            esc.set(align="left", bold=True, double_height=True, double_width=False)
            measurement = item.measurement or "--"
            esc.text(f"{item.name} -- {measurement}\n")
            esc.set(align="left", bold=False, double_height=False, double_width=False)
            esc.text(sep + "\n")
            table = item.table or payload.table or "--"
            esc.text(f"Стіл: {table}\n")
            esc.text(sep + "\n")
            guest = item.guest or payload.guest or "--"
            esc.text(f"Гість: {guest}\n")
            if item.note:
                esc.text(sep + "\n")
                esc.text(f"Коментар: {item.note}\n")
            # Two separators close the block — visually distinct from the
            # single-line separators inside it.
            esc.text(sep + "\n")
            esc.text(sep + "\n")

        if payload.comment:
            esc.text(f"Коментар: {payload.comment}\n")
            esc.text(sep + "\n")

        # Tear-off padding — the rail clip needs ~3 cm of blank paper to
        # hold the ticket up, otherwise a 1-item ticket curls under.
        esc.text("\n\n\n\n\n\n\n\n\n\n")
        esc.cut()

    try:
        await device.enqueue(_job)
    except PrinterUnavailable as exc:
        # Surface the structured code so the frontend can switch on it
        # ("out_of_paper" → "Закінчився папір", "cover_open" → "Закрийте кришку").
        raise HTTPException(
            status_code=503,
            detail={"code": getattr(exc, "code", "unavailable"), "message": str(exc)},
        )
    return {"status": "printed", "printer_id": reg.descriptor.id}
