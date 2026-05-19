"""GET /health — no auth, used by web apps to probe for the manager.

Returns the live status of each configured device:
- `connected`     — device responds; printing should work
- `not_configured` — `enabled: false` in config.yaml
- `unavailable`   — enabled but failed to connect (hardware unplugged,
                    wrong vendor/product id, driver missing, etc.)
"""

from fastapi import APIRouter, Request

router = APIRouter()


def _device_status(app_state, key: str, cfg_key: str) -> str:
    cfg = (app_state.config.get("devices") or {}).get(cfg_key) or {}
    if not cfg.get("enabled"):
        return "not_configured"
    device = getattr(app_state, f"{cfg_key}_printer", None)
    if device is None:
        return "unavailable"
    return "connected" if device.is_connected() else "unavailable"


@router.get("/health")
async def health(request: Request):
    state = request.app.state
    return {
        "status": "ok",
        "version": "0.1.0",
        "devices": {
            "receipt": _device_status(state, "receipt", "receipt"),
            "label": _device_status(state, "label", "label"),
            "terminal": _device_status(state, "terminal", "terminal"),
        },
    }
