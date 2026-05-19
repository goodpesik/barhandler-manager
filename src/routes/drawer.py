"""POST /drawer/open — pulse the cash-drawer connector on the receipt
printer. Silent success when no drawer is wired (graceful).
"""

from fastapi import APIRouter, Request

router = APIRouter()


@router.post("/open")
async def open_drawer(request: Request):
    printer = request.app.state.receipt_printer
    if printer is None:
        return {"status": "opened"}  # no printer configured — still acknowledge
    await printer.open_drawer()
    return {"status": "opened"}
