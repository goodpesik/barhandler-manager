from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from src.routes import health, devices, print_routes, terminal

API_KEY_HEADER = APIKeyHeader(name="X-Api-Key", auto_error=False)

def create_app(config: dict) -> FastAPI:
    api_key = config["server"]["api_key"]

    app = FastAPI(title="Barhandler Manager", version="0.1.0")

    async def verify_key(key: str = Security(API_KEY_HEADER)):
        if key != api_key:
            raise HTTPException(status_code=401, detail="Invalid API key")

    app.include_router(health.router)
    app.include_router(devices.router, prefix="/devices", dependencies=[Depends(verify_key)])
    app.include_router(print_routes.router, prefix="/print", dependencies=[Depends(verify_key)])
    app.include_router(terminal.router, prefix="/terminal", dependencies=[Depends(verify_key)])

    return app
