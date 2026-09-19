"""GET /health — no auth, used by web apps to probe for the manager and
to render per-printer status pills in their UI.

For each registered printer we report:
  - `connected`     — device is reachable
  - `disconnected`  — registered but not currently open (lazy connect)
  - `unavailable`   — open attempt failed (hardware unplugged / busy)

The response also embeds the same `version` / `latest_version` /
`has_update` block exposed by GET /version so callers that already
poll /health don't need a second round-trip to decide whether to
show an "update available" modal.
"""

from fastapi import APIRouter, Request

from src.services.busy import busy_refusal

router = APIRouter()


@router.get("/busy")
async def busy(request: Request) -> dict:
    """Чи менеджер посеред незворотної роботи — БЕЗ ключа, як і /health.

    BH-164. `POST /system/update` питає це саме в собі, але цього мало:
    інсталятори вбивають процес не тоді, коли натиснули кнопку, а тоді, коли
    ЛЮДИНА дійде до кроку установки в майстрі. Між цими двома моментами тепер
    офіційно до 20 хвилин — за них цілком може початись оплата карткою.

    Тому питати мусять і самі інсталятори, безпосередньо перед тим, як убивати:
    `installers/barhandler-setup.iss` (PrepareToInstall), `mac-postinstall.sh`
    та `install.sh`. Ключа в них немає, а відповідь не розкриває нічого, крім
    «зайнятий/вільний» — рівно як /health і /version.
    """
    refusal = busy_refusal(request)
    if refusal is None:
        return {"busy": False, "message": "", "reasons": []}
    return {
        "busy": True,
        "message": refusal["message"],
        "reasons": refusal["busy"],
    }


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

    checker = getattr(state, "update_checker", None)
    if checker is not None:
        update = checker.snapshot()
    else:
        cfg = getattr(state, "config", {})
        update = {
            "version": cfg.get("version", "unknown"),
            "latest_version": None,
            "has_update": False,
            "release_url": None,
            "release_published_at": None,
            "checked_at": None,
        }

    return {
        "status": "ok",
        **update,
        "printers": printers,
    }
