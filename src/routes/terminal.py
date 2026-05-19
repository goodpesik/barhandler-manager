"""POST /terminal/charge — Phase 2 stub. Monobank acquiring over
Ethernet will land here; protocol TBD.
"""

from fastapi import APIRouter

router = APIRouter()


@router.post("/charge")
async def charge():
    return {"status": "not_implemented"}
