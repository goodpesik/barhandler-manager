"""FastAPI app factory: wires auth + lifespan + routers.

Lifespan owns the printer device — connects on startup so /health can
report `connected` immediately, disconnects on shutdown so the asyncio
worker task and underlying USB handle are released cleanly.
"""

from contextlib import asynccontextmanager
import logging

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader

from src.devices.printer import PrinterDevice
from src.routes import devices, drawer, health, print_routes, terminal

logger = logging.getLogger(__name__)

API_KEY_HEADER = APIKeyHeader(name="X-Api-Key", auto_error=False)


def create_app(config: dict) -> FastAPI:
    api_key = config["server"]["api_key"]
    receipt_cfg = config.get("devices", {}).get("receipt") or {}
    receipt_printer = PrinterDevice("receipt", receipt_cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = config
        app.state.receipt_printer = receipt_printer
        if receipt_cfg.get("enabled"):
            ok = await receipt_printer.connect()
            if not ok:
                logger.warning("receipt printer configured but unavailable — /health will report 'unavailable'")
        yield
        await receipt_printer.disconnect()

    app = FastAPI(title="Barhandler Manager", version="0.1.0", lifespan=lifespan)

    async def verify_key(key: str = Security(API_KEY_HEADER)):
        if key != api_key:
            raise HTTPException(status_code=401, detail="Invalid API key")

    app.include_router(health.router)
    app.include_router(devices.router, prefix="/devices", dependencies=[Depends(verify_key)])
    app.include_router(print_routes.router, prefix="/print", dependencies=[Depends(verify_key)])
    app.include_router(drawer.router, prefix="/drawer", dependencies=[Depends(verify_key)])
    app.include_router(terminal.router, prefix="/terminal", dependencies=[Depends(verify_key)])

    return app
