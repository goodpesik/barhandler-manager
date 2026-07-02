"""Italian RT fiscal-printer routes — /fiscal/it/*  (PET-237 Phase C).

These endpoints drive an Epson RT printer over its EpsonFPMate web service
(HTTP + fiscal ePOS-Print XML), NOT the ESC/POS path in `print_routes.py`.
The printer is still selected by `printer_id` (or the first registered
`receipt`-kind printer as a fallback), but we resolve it to its **network
address** and POST XML — we never open the raw ESC/POS socket, so we bypass
`registry.get_device()` and read the descriptor directly.

Request-body field convention: **snake_case**, matching the rest of the
manager (`FiscalReceipt.total_sum`, `LabelPayload.image_base64`, …). The
NestJS server must send snake_case bodies. Response bodies follow the PET-237
contract verbatim (camelCase: receiptId / receiptNumber / reportNumber /
lastZAt / blocked / raw).

The blocking network call runs in a worker thread (`asyncio.to_thread`) so the
event loop isn't stalled while the printer prints.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.devices.registry import UnknownPrinter
from src.models.printer import PrinterKind
from src.services import fiscal_it

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models (snake_case — manager convention)
# ---------------------------------------------------------------------------


class ItDocumentItem(BaseModel):
    name: str
    quantity: float = Field(gt=0)
    unit_price: float = Field(ge=0)
    total_price: float = Field(ge=0)
    iva_rate: float = Field(ge=0)          # VAT percentage, e.g. 22 / 10 / 4 / 0
    department: Optional[int] = None       # reparto; derived from iva_rate if null


class ItPaymentBody(BaseModel):
    type: str                              # caller's tender key, resolved via payment_type_map
    amount: float = Field(ge=0)


class ItDocumentPayload(BaseModel):
    items: list[ItDocumentItem] = Field(min_length=1)
    payment: ItPaymentBody
    payment_type_map: Optional[dict[str, int]] = None
    is_refund: bool = False


class ReprintPayload(BaseModel):
    receipt_number: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Printer resolution
# ---------------------------------------------------------------------------


def _resolve_rt_host(request: Request, printer_id: Optional[str]) -> tuple[str, str, int]:
    """Resolve `printer_id` → (printer_id, host, http_port) for the RT printer.

    Falls back to the first registered `fiscal_it`-kind printer when no id is
    given. The port comes from the registration's network address (the RT
    printer is registered by IP:port via POST /devices/register-manual — 80 on
    real hardware, e.g. 8095 for the emulator), NOT the ESC/POS default. Raises
    the same HTTP error shapes (503 unregistered / 404 unknown / 422 not-a-
    network-printer).
    """
    registry = request.app.state.registry
    if printer_id is None:
        reg = registry.for_kind(PrinterKind.fiscal_it)
        if reg is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "no 'fiscal_it' printer registered — POST /devices/register-manual "
                    "with the RT printer's IP:port first"
                ),
            )
    else:
        try:
            reg = registry.get_registration(printer_id)
        except UnknownPrinter:
            raise HTTPException(status_code=404, detail=f"unknown printer_id: {printer_id}")

    net = reg.descriptor.network
    if net is None:
        raise HTTPException(
            status_code=422,
            detail="RT fiscal printing requires a network printer (no network address on this registration)",
        )
    # A manually-registered RT printer carries the fpmate HTTP port; only fall
    # back to 80 if it somehow still holds the ESC/POS default (9100).
    port = net.port if net.port and net.port != 9100 else fiscal_it.DEFAULT_HTTP_PORT
    return reg.descriptor.id, net.host, port


def _raise_from_fiscal_error(exc: fiscal_it.FiscalItError):
    """Map a FiscalItError to an HTTP error carrying the structured code —
    same {code, message} envelope the ESC/POS routes use for PrinterUnavailable
    so the frontend can switch on it (PRINTER ERROR 17 → clear message)."""
    status = 503 if exc.code == "printer_unreachable" else 502
    raise HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": str(exc), "raw": exc.raw},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/document")
async def fiscal_it_document(
    payload: ItDocumentPayload,
    request: Request,
    printer_id: Optional[str] = Query(default=None),
):
    """Print a Documento Commerciale (sale, or refund when is_refund=true)."""
    pid, host, port = _resolve_rt_host(request, printer_id)
    document = fiscal_it.ItDocument(
        items=[
            fiscal_it.ItItem(
                name=i.name,
                quantity=i.quantity,
                unit_price=i.unit_price,
                total_price=i.total_price,
                iva_rate=i.iva_rate,
                department=i.department,
            )
            for i in payload.items
        ],
        payment=fiscal_it.ItPayment(type=payload.payment.type, amount=payload.payment.amount),
        payment_type_map=payload.payment_type_map,
        is_refund=payload.is_refund,
    )
    try:
        return await asyncio.to_thread(
            fiscal_it.print_commercial_document, host, document, port=port
        )
    except fiscal_it.FiscalItError as exc:
        _raise_from_fiscal_error(exc)


@router.post("/z")
async def fiscal_it_z(
    request: Request,
    printer_id: Optional[str] = Query(default=None),
):
    """Daily close (Z report)."""
    pid, host, port = _resolve_rt_host(request, printer_id)
    try:
        return await asyncio.to_thread(fiscal_it.run_z_report, host, port=port)
    except fiscal_it.FiscalItError as exc:
        _raise_from_fiscal_error(exc)


@router.post("/x")
async def fiscal_it_x(
    request: Request,
    printer_id: Optional[str] = Query(default=None),
):
    """X read (non-resetting daily read)."""
    pid, host, port = _resolve_rt_host(request, printer_id)
    try:
        return await asyncio.to_thread(fiscal_it.run_x_report, host, port=port)
    except fiscal_it.FiscalItError as exc:
        _raise_from_fiscal_error(exc)


@router.post("/reprint")
async def fiscal_it_reprint(
    request: Request,
    payload: ReprintPayload,
    printer_id: Optional[str] = Query(default=None),
):
    """Reprint a duplicate (copia) of an existing fiscal document by number —
    the RT printer reprints from its own memory (no re-formed receipt)."""
    pid, host, port = _resolve_rt_host(request, printer_id)
    try:
        return await asyncio.to_thread(
            fiscal_it.run_reprint, host, payload.receipt_number, port=port
        )
    except fiscal_it.FiscalItError as exc:
        _raise_from_fiscal_error(exc)


@router.get("/status")
async def fiscal_it_status(
    request: Request,
    printer_id: Optional[str] = Query(default=None),
):
    """RT status — {lastZAt, blocked, raw}. blocked=true when the last Z is
    older than 48h (the printer would refuse sales)."""
    pid, host, port = _resolve_rt_host(request, printer_id)
    try:
        return await asyncio.to_thread(fiscal_it.query_status, host, port=port)
    except fiscal_it.FiscalItError as exc:
        _raise_from_fiscal_error(exc)
