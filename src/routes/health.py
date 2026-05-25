"""GET /health — no auth, used by web apps to probe for the manager and
to render per-printer status pills in their UI.

For each registered printer we report:
  - `connected`     — device is reachable
  - `disconnected`  — registered but not currently open (lazy connect)
  - `unavailable`   — open attempt failed (hardware unplugged / busy)
"""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    state = request.app.state
    registry = getattr(state, "registry", None)
    printers = []
    if registry is not None:
        for reg in registry.all_registrations():
            device = registry._devices.get(reg.descriptor.id)  # noqa: SLF001
            if device is None:
                status = "disconnected"
            elif device.is_connected():
                status = "connected"
            else:
                status = "unavailable"
            printers.append({
                "id": reg.descriptor.id,
                "kind": reg.kind.value if hasattr(reg.kind, "value") else reg.kind,
                "label": reg.nickname or reg.descriptor.label,
                "transport": (
                    reg.descriptor.transport.value
                    if hasattr(reg.descriptor.transport, "value")
                    else reg.descriptor.transport
                ),
                "status": status,
            })
    return {
        "status": "ok",
        "version": "0.3.1",
        "printers": printers,
    }
