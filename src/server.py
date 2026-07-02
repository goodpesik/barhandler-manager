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
from src.routes import (
    dashboard, devices, drawer, fiscal_it, health, print_routes, system, terminal, version,
)
from src.services.update_check import UpdateChecker

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

    async def _printer_connect_watcher() -> None:
        """Connect-ONLY watcher — the safe inverse of the old heartbeat. Every
        15 s it OPENS any registered printer that isn't currently connected
        (get_device builds / reconnects the handle). It NEVER disconnects, so
        it can't kill a working printer the way the status-probe heartbeat did:
        a connected handle is left untouched; a printer that's off stays
        disconnected until it's powered on, then the next tick opens it (the
        dashboard flips to 'connected' on its own, no print needed). Runs once
        immediately so a printer is ready right after startup."""
        while True:
            for reg in registry.all_registrations():
                try:
                    await registry.get_device(reg.descriptor.id)
                except Exception:
                    pass
            await asyncio.sleep(15)

    # Read the installed VERSION once at startup — this is what the
    # operator's `update.sh` writes after a release. Don't re-read on
    # every request; if the file changes mid-flight the process is about
    # to restart anyway.
    version_path = Path(__file__).resolve().parent.parent / "VERSION"
    current_version = version_path.read_text().strip() if version_path.exists() else "0.0.0"
    # Expose so health/version routes can fall back when the checker
    # hasn't started yet.
    config["version"] = current_version

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = config
        app.state.registry = registry
        app.state.terminal_registry = terminal_registry
        registry.load()
        terminal_registry.load()
        connect_watcher = asyncio.create_task(
            _printer_connect_watcher(), name="printer-connect",
        )

        update_checker = UpdateChecker(current_version=current_version)
        app.state.update_checker = update_checker
        update_task = asyncio.create_task(
            update_checker.run_forever(), name="update-check",
        )

        # Optional uplink — streams logs + business events to the central
        # barhandler-manager-logs server. Fully opt-in via config.yaml.
        uplink = None
        uplink_cfg = config.get("uplink", {})
        if uplink_cfg.get("enabled"):
            from src.services.log_uplink import (
                LogUplinkClient, get_or_create_install_id, set_active,
            )
            install_id_path = Path(__file__).resolve().parent.parent / "install_id.txt"
            install_id = get_or_create_install_id(install_id_path)
            version_path = Path(__file__).resolve().parent.parent / "VERSION"
            version = version_path.read_text().strip() if version_path.exists() else "0.0.0"
            uplink = LogUplinkClient(uplink_cfg)
            uplink.attach_handler_to_root()
            from src.services.diagnostics import make_callback
            uplink.set_diagnostics_callback(make_callback(config))
            set_active(uplink)
            app.state.uplink = uplink
            asyncio.create_task(uplink.start(install_id, version))

        yield

        if uplink is not None:
            await uplink.stop()
        await update_checker.stop()
        update_task.cancel()
        connect_watcher.cancel()
        await registry.disconnect_all()

    app = FastAPI(title="Barhandler Manager", version="0.3.52", lifespan=lifespan)

    # CORS — the browser drives this service directly from the
    # BarHandler/FitStudio web apps (and from any future local web UI).
    # All callers live on the same host so a permissive policy is safe;
    # the X-Api-Key middleware below is what actually gates access.
    cors_origins = config["server"].get("cors_origins") or [
        "http://localhost:4115",      # bar-handler-app dev server
        "http://localhost:4200",      # generic Angular default
        "http://localhost:8080",
        "https://barhandler.com",
    ]
    # Match every Firebase Hosting subdomain (production sites, preview
    # channels, and *.firebaseapp.com fallback URLs) so newly-deployed
    # projects don't need a manager-side config bump to talk to the
    # local bridge. Preview channels use a `--` separator like
    # `bar-handler--preview-abc.web.app`, which the regex below covers.
    # Operators can still override with their own cors_origin_regex
    # in config.yaml if they need something narrower.
    # Default regex covers:
    #  - any Firebase Hosting subdomain (production + preview channels)
    #  - any *.firebaseapp.com fallback URL
    #  - tenant deployments on barhandler.com / petshandler.com /
    #    fitstudiocrm.com — apex domains AND any subdomain depth
    #    (biergarten-lviv.barhandler.com, app.fitstudiocrm.com, etc.)
    #  - localhost on any port via http or https (covers dev servers
    #    and Capacitor Android wrappers, which use `https://localhost`)
    #  - capacitor://localhost (Capacitor 4+ native bridge)
    # X-Api-Key middleware still gates every request, so liberal CORS
    # here is safe — the key is what actually authorises calls.
    cors_origin_regex = config["server"].get("cors_origin_regex") or (
        r"^("
        r"https?://localhost(:\d+)?"
        r"|capacitor://localhost"
        r"|https://[a-zA-Z0-9-]+\.(web\.app|firebaseapp\.com)"
        r"|https://([a-zA-Z0-9-]+\.)*barhandler\.com"
        r"|https://([a-zA-Z0-9-]+\.)*petshandler\.com"
        r"|https://([a-zA-Z0-9-]+\.)*fitstudiocrm\.com"
        r")$"
    )
    # NB: `allow_credentials=True` + `allow_headers=["*"]` is invalid
    # per the CORS spec — wildcard `Access-Control-Allow-Headers` is
    # only permitted when credentials are NOT in play. Starlette
    # silently rejects preflights from some clients (observed on
    # Android Chrome inside an emulator) when this combo is set,
    # surfacing as "HTTP status of preflight request didn't indicate
    # success" with no explicit error.
    #
    # We use X-Api-Key (a custom header, not a cookie) for auth, so
    # credentials=True buys nothing — the browser doesn't carry our
    # auth bit via cookies anyway. Setting it to False lets the
    # wildcard headers + wildcard methods stay legal under the spec
    # and Starlette stops being picky.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=cors_origin_regex,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # CORS rejections are otherwise silent — uvicorn logs a `400 Bad
    # Request` for the preflight but doesn't say WHY, leaving the
    # operator to guess "is this CORS, auth, or a real bug?" Log each
    # cross-origin OPTIONS with its outcome so the dashboard's bhm.log
    # tab shows exactly which Origin got rejected and what's allowed.
    import logging as _logging
    import re as _re
    _cors_logger = _logging.getLogger("src.cors")
    _cors_origins_set = set(cors_origins)
    _cors_regex_compiled = _re.compile(cors_origin_regex) if cors_origin_regex else None
    _cors_logger.info(
        "CORS allowed_origins=%s allowed_regex=%r",
        cors_origins, cors_origin_regex,
    )

    from starlette.responses import Response as _StarletteResponse

    import time as _time

    # Tracks the most recent Origin from a remote (non-localhost) caller
    # so the uplink toggle can pick up the tenant subdomain without
    # asking the operator to type it. localhost / 127.x / capacitor are
    # ignored because they're the operator's own browser hitting the
    # dashboard, not a tenant PWA.
    app.state.last_tenant_origin = ""
    _LOCALHOST_ORIGIN_RE = _re.compile(
        r"^(https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?|capacitor://.*)$",
        _re.IGNORECASE,
    )

    @app.middleware("http")
    async def _request_uplink(request, call_next):
        """Emit each HTTP request as a business event so the central
        logs server gets per-tenant access audit. Wraps after
        cors_and_pna so we capture the real response (after CORS short-
        circuit decisions). Fire-and-forget — never affects the
        response.

        Also remembers the most recent non-localhost Origin so the
        uplink modal can auto-detect tenant.
        """
        started = _time.perf_counter()
        response = await call_next(request)
        origin = request.headers.get("origin", "")
        if origin and not _LOCALHOST_ORIGIN_RE.match(origin):
            app.state.last_tenant_origin = origin
        try:
            from src.services.log_uplink import emit_event
            emit_event(
                "request_seen",
                origin=origin,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=int((_time.perf_counter() - started) * 1000),
            )
        except Exception:
            pass
        return response

    @app.middleware("http")
    async def cors_and_pna(request, call_next):
        origin = request.headers.get("origin")
        is_preflight = request.method == "OPTIONS" and origin is not None
        is_pna_preflight = (
            is_preflight
            and request.headers.get("access-control-request-private-network") == "true"
        )
        allowed = origin is not None and (
            origin in _cors_origins_set
            or (_cors_regex_compiled is not None
                and _cors_regex_compiled.match(origin) is not None)
        )

        # Private Network Access (PNA, Chrome 117+) — fetches from a
        # "public" origin to a "private" target (anything in 127/8)
        # need an extra preflight signal: `Access-Control-Request-
        # Private-Network: true` from the browser, and we MUST echo
        # `Access-Control-Allow-Private-Network: true` back. Starlette's
        # CORSMiddleware knows about PNA but refuses it by default
        # (returns 400 "Disallowed CORS private-network"), and exposes
        # no flag to opt in. So we short-circuit the PNA preflight
        # ourselves before CORSMiddleware sees it.
        if is_pna_preflight and allowed:
            requested_method = request.headers.get(
                "access-control-request-method", "GET",
            )
            requested_headers = request.headers.get(
                "access-control-request-headers", "*",
            )
            _cors_logger.info(
                "PNA preflight from origin=%s path=%s — allowing",
                origin, request.url.path,
            )
            return _StarletteResponse(
                status_code=200,
                headers={
                    "access-control-allow-origin": origin,
                    "access-control-allow-methods": requested_method,
                    "access-control-allow-headers": requested_headers,
                    "access-control-allow-private-network": "true",
                    "access-control-max-age": "600",
                    "vary": "Origin",
                },
            )

        if is_preflight and not allowed:
            _cors_logger.warning(
                "preflight REJECTED origin=%s path=%s "
                "(not in allow_origins and doesn't match regex)",
                origin, request.url.path,
            )

        return await call_next(request)

    async def verify_key(key: str = Security(API_KEY_HEADER)):
        if key != api_key:
            raise HTTPException(status_code=401, detail="Invalid API key")

    app.include_router(health.router)
    app.include_router(version.router)
    app.include_router(dashboard.router)
    app.include_router(devices.router, prefix="/devices", dependencies=[Depends(verify_key)])
    app.include_router(print_routes.router, prefix="/print", dependencies=[Depends(verify_key)])
    app.include_router(fiscal_it.router, prefix="/fiscal/it", dependencies=[Depends(verify_key)])
    app.include_router(drawer.router, prefix="/drawer", dependencies=[Depends(verify_key)])
    app.include_router(terminal.router, prefix="/terminal", dependencies=[Depends(verify_key)])
    app.include_router(system.router, prefix="/system", dependencies=[Depends(verify_key)])

    return app
