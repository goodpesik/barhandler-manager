"""FastAPI app factory.

Lifespan owns the PrinterRegistry: load `printers.json` at startup, close
all open device handles on shutdown. Printers are NOT connected eagerly —
connections happen on the first print to that ID.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import logging

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader

from src.constants import DEFAULT_API_KEY
from src.devices.registry import PrinterRegistry
from src.devices.terminal_registry import TerminalRegistry
from src.routes import dashboard, devices, drawer, health, print_routes, system, terminal

logger = logging.getLogger(__name__)

API_KEY_HEADER = APIKeyHeader(name="X-Api-Key", auto_error=False)


def create_app(config: dict) -> FastAPI:
    # The handshake key is the embedded constant — `config.yaml` may
    # override it for shops running multiple isolated POS apps on one
    # host, but the default ships with every install.
    api_key = config["server"].get("api_key") or DEFAULT_API_KEY
    registry_path = Path(config["server"].get("registry_path", "printers.json"))
    registry = PrinterRegistry(path=registry_path)
    terminal_registry_path = Path(
        config["server"].get("terminal_registry_path", "terminals.json"),
    )
    terminal_registry = TerminalRegistry(path=terminal_registry_path)

    async def _printer_heartbeat() -> None:
        """Every 30 s probe each connected printer. On failure, disconnect
        the stale handle so /health reports 'unavailable' immediately."""
        while True:
            await asyncio.sleep(30)
            for printer_id, device in list(registry._devices.items()):  # noqa: SLF001
                if device.is_connected():
                    try:
                        reachable = await device.async_probe()
                    except Exception:
                        reachable = False
                    if not reachable:
                        logger.info("[heartbeat] %s unreachable — disconnecting", printer_id)
                        await device.disconnect()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = config
        app.state.registry = registry
        app.state.terminal_registry = terminal_registry
        registry.load()
        terminal_registry.load()
        heartbeat = asyncio.create_task(_printer_heartbeat(), name="printer-heartbeat")
        yield
        heartbeat.cancel()
        await registry.disconnect_all()

    app = FastAPI(title="Barhandler Manager", version="0.3.20", lifespan=lifespan)

    # CORS — the browser drives this service directly from the
    # BarHandler/FitStudio web apps (and from any future local web UI).
    # All callers live on the same host so a permissive policy is safe;
    # the X-Api-Key middleware below is what actually gates access.
    cors_origins = config["server"].get("cors_origins") or [
        "http://localhost:4115",      # bar-handler-app dev server
        "http://localhost:4200",      # generic Angular default
        "http://localhost:8080",
        "https://bar-handler.web.app",
        "https://barhandler.com",
    ]
    cors_origin_regex = config["server"].get("cors_origin_regex")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=cors_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    async def verify_key(key: str = Security(API_KEY_HEADER)):
        if key != api_key:
            raise HTTPException(status_code=401, detail="Invalid API key")

    app.include_router(health.router)
    app.include_router(dashboard.router)
    app.include_router(devices.router, prefix="/devices", dependencies=[Depends(verify_key)])
    app.include_router(print_routes.router, prefix="/print", dependencies=[Depends(verify_key)])
    app.include_router(drawer.router, prefix="/drawer", dependencies=[Depends(verify_key)])
    app.include_router(terminal.router, prefix="/terminal", dependencies=[Depends(verify_key)])
    app.include_router(system.router, prefix="/system", dependencies=[Depends(verify_key)])

    return app
