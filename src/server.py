"""FastAPI app factory.

Lifespan owns the PrinterRegistry: load `printers.json` at startup, close
all open device handles on shutdown. Printers are NOT connected eagerly —
connections happen on the first print to that ID.
"""

from contextlib import asynccontextmanager
from pathlib import Path
import logging

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader

from src.constants import DEFAULT_API_KEY
from src.devices.registry import PrinterRegistry
from src.routes import devices, drawer, health, print_routes, terminal

logger = logging.getLogger(__name__)

API_KEY_HEADER = APIKeyHeader(name="X-Api-Key", auto_error=False)


def create_app(config: dict) -> FastAPI:
    # The handshake key is the embedded constant — `config.yaml` may
    # override it for shops running multiple isolated POS apps on one
    # host, but the default ships with every install.
    api_key = config["server"].get("api_key") or DEFAULT_API_KEY
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    async def verify_key(key: str = Security(API_KEY_HEADER)):
        if key != api_key:
            raise HTTPException(status_code=401, detail="Invalid API key")

    app.include_router(health.router)
    app.include_router(devices.router, prefix="/devices", dependencies=[Depends(verify_key)])
    app.include_router(print_routes.router, prefix="/print", dependencies=[Depends(verify_key)])
    app.include_router(drawer.router, prefix="/drawer", dependencies=[Depends(verify_key)])
    app.include_router(terminal.router, prefix="/terminal", dependencies=[Depends(verify_key)])

    return app
