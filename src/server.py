"""FastAPI app factory.

Lifespan owns the PrinterRegistry: load `printers.json` at startup, close
all open device handles on shutdown. Printers are NOT connected eagerly —
connections happen on the first print to that ID.
"""

from contextlib import asynccontextmanager
from pathlib import Path
import logging

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader

from src.devices.registry import PrinterRegistry
from src.routes import devices, drawer, health, print_routes, terminal

logger = logging.getLogger(__name__)

API_KEY_HEADER = APIKeyHeader(name="X-Api-Key", auto_error=False)


def create_app(config: dict) -> FastAPI:
    api_key = config["server"]["api_key"]
    registry_path = Path(config["server"].get("registry_path", "printers.json"))
    registry = PrinterRegistry(path=registry_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = config
        app.state.registry = registry
        registry.load()
        yield
        await registry.disconnect_all()

    app = FastAPI(title="Barhandler Manager", version="0.2.0", lifespan=lifespan)

    async def verify_key(key: str = Security(API_KEY_HEADER)):
        if key != api_key:
            raise HTTPException(status_code=401, detail="Invalid API key")

    app.include_router(health.router)
    app.include_router(devices.router, prefix="/devices", dependencies=[Depends(verify_key)])
    app.include_router(print_routes.router, prefix="/print", dependencies=[Depends(verify_key)])
    app.include_router(drawer.router, prefix="/drawer", dependencies=[Depends(verify_key)])
    app.include_router(terminal.router, prefix="/terminal", dependencies=[Depends(verify_key)])

    return app
