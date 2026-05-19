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


# A handful of cyrillic tables every common ESC/POS firmware exposes.
# (codec name in Python, table number sent via ESC t <n>).
_CYR_TABLES = [
    ("cp866", 17),
    ("cp1251", 46),
    ("cp1251", 33),
    ("cp1125", 44),
    ("cp855", 34),
    ("iso8859_5", 38),
]


@router.post("/{printer_id}/probe-codepage")
async def probe_codepage(printer_id: str, request: Request) -> dict:
    """Print a labelled cyrillic sample with each common code page so the
    operator can eyeball the receipt and pick the encoding that reads
    correctly. The chosen one then goes into the printer's registration
    via POST /devices/register.
    """
    registry = _registry(request)
    try:
        registry.get_registration(printer_id)
    except UnknownPrinter as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        device = await registry.get_device(printer_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"printer connect failed: {exc}")
    if not device.is_connected():
        raise HTTPException(status_code=503, detail="printer_unavailable")

    sample = "Готівка їєіґ — тест"

    async def _job(esc):
        esc.set(align="left", bold=False)
        esc.text("=== code page probe ===\n")
        for codec, table in _CYR_TABLES:
            label = f"[{codec} / table {table}]"
            esc._raw(b"\x1bt" + bytes([table]))
            try:
                encoded = sample.encode(codec, errors="replace")
            except LookupError:
                continue
            esc._raw(label.encode("ascii", errors="replace") + b"\n")
            esc._raw(encoded + b"\n")
        # restore a reasonable default for the cut + next print
        esc._raw(b"\x1bt" + bytes([0]))
        esc.text("=======================\n\n\n")
        esc.cut()

    try:
        await device.enqueue(_job)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "printed", "tables_tried": [{"codec": c, "table": t} for c, t in _CYR_TABLES]}


@router.post("/{printer_id}/test-print")
async def test_print(printer_id: str, request: Request) -> dict:
    """Print a friendly demo receipt using the printer's current settings.

    The frontend uses this on the Settings → Printers screen to let the
    operator visually confirm a registration before saving it for real
    transactions ("залишити цей режим" / "спробувати інший"). The output
    exercises every text style the production renderers use — header,
    body, bold total, footer with Ukrainian-specific glyphs — so any
    issue surfaces in one print.
    """
    registry = _registry(request)
    try:
        reg = registry.get_registration(printer_id)
    except UnknownPrinter as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        device = await registry.get_device(printer_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"printer connect failed: {exc}")
    if not device.is_connected():
        raise HTTPException(status_code=503, detail="printer_unavailable")

    chars = reg.chars_per_line

    async def _job(esc):
        esc.set(align="center", bold=True)
        esc.text(f"Тестовий друк\n")
        esc.set(align="left", bold=False)
        esc.text("-" * chars + "\n")
        esc.text(f"Принтер: {reg.nickname or reg.descriptor.label}\n")
        esc.text(f"Режим:   {reg.render_mode}\n")
        if reg.code_page:
            esc.text(f"Page:    {reg.code_page}\n")
        esc.text(f"Папір:   {reg.paper_width}мм ({chars} символів)\n")
        esc.text("-" * chars + "\n")
        # Visual width gauge — should fill exactly to the right edge.
        ruler = "0123456789" * ((chars // 10) + 1)
        esc.text(ruler[:chars] + "\n")
        esc.text("-" * chars + "\n")
        # Style sample
        esc.text("Звичайний текст\n")
        esc.set(bold=True)
        esc.text("Жирний текст\n")
        esc.set(bold=False)
        esc.set(double_height=True)
        esc.text("Великий\n")
        esc.set(double_height=False)
        esc.text("-" * chars + "\n")
        # Ukrainian sanity: each line is a hard test for code-page / glyph
        esc.text("Деруни      2 x 100.00 ГРН\n")
        esc.text("Реберця     1 x 300.00 ГРН\n")
        esc.text("Готівка / Картка / Еквайринг\n")
        esc.text("Українські:  і ї є ґ І Ї Є Ґ\n")
        esc.text("Сума:        1 234.56 грн\n")
        esc.text("Дата:        21.04.2026 09:15\n")
        esc.text("Тире/лапки:  — «текст»\n")
        esc.text("-" * chars + "\n")
        esc.set(align="center")
        esc.text("Якщо всі рядки читаються\n")
        esc.text("без ?, |, або зайвих символів —\n")
        esc.text("обирай цей режим у Settings.\n")
        esc.set(align="left")
        esc.text("\n\n")
        esc.cut()

    try:
        await device.enqueue(_job)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {
        "status": "printed",
        "printer_id": printer_id,
        "render_mode": reg.render_mode,
        "code_page": reg.code_page,
    }
