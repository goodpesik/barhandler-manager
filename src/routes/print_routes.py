"""Print routes:
- POST /print/receipt — internal (non-fiscal) JSON receipt.
- POST /print/fiscal  — unified fiscal receipt rendered Vchasno-style for both
                        Checkbox and Vchasno Kasa payloads.
- POST /print/text    — raw plaintext (e.g. Checkbox /api/v1/receipts/{id}/text).

Module name is `print_routes` because `print` is a Python builtin.
"""

from pydantic import BaseModel

from fastapi import APIRouter, HTTPException, Request

from src.devices.printer import PrinterUnavailable
from src.models.fiscal_receipt import FiscalReceipt
from src.models.receipt import ReceiptPayload
from src.services.fiscal_receipt import render_fiscal_receipt
from src.services.receipt import render_receipt

router = APIRouter()


def _get_printer_or_503(request: Request):
    printer = request.app.state.receipt_printer
    if printer is None or not printer.is_connected():
        raise HTTPException(status_code=503, detail="printer_unavailable")
    return printer


def _maybe_open_drawer(esc, printer, open_drawer: bool) -> None:
    if open_drawer and printer.drawer_pin is not None:
        try:
            esc.cashdraw(int(printer.drawer_pin))
        except Exception:
            # graceful: drawer wiring optional
            pass


@router.post("/receipt")
async def print_receipt(payload: ReceiptPayload, request: Request):
    printer = _get_printer_or_503(request)
    chars = printer.chars_per_line

    async def _job(esc):
        render_receipt(esc, payload, chars_per_line=chars)
        _maybe_open_drawer(esc, printer, payload.open_drawer)

    try:
        await printer.enqueue(_job)
    except PrinterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "printed"}


@router.post("/fiscal")
async def print_fiscal(payload: FiscalReceipt, request: Request):
    printer = _get_printer_or_503(request)
    chars = printer.chars_per_line

    async def _job(esc):
        render_fiscal_receipt(esc, payload, chars_per_line=chars)
        _maybe_open_drawer(esc, printer, payload.open_drawer)

    try:
        await printer.enqueue(_job)
    except PrinterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "printed"}


class RawTextPayload(BaseModel):
    text: str
    open_drawer: bool = False


@router.post("/text")
async def print_text(payload: RawTextPayload, request: Request):
    """Print a pre-rendered text receipt verbatim.

    Use this for Checkbox: web app calls
    GET https://api.checkbox.in.ua/api/v1/receipts/{id}/text?width=<N>
    (text/plain, already legal-compliant per regulation №329), forwards the
    body here.
    """
    printer = _get_printer_or_503(request)

    async def _job(esc):
        # Ensure a clean state before raw text and finish with a cut.
        esc.set(align="left", bold=False, double_height=False, double_width=False)
        esc.text(payload.text.rstrip() + "\n\n\n")
        esc.cut()
        _maybe_open_drawer(esc, printer, payload.open_drawer)

    try:
        await printer.enqueue(_job)
    except PrinterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "printed"}
