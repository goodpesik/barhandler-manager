"""GET /version — no auth, returns current manager version and whether a
newer release is available on GitHub. Designed to be polled by browser
PWAs so they can pop a "new version, click to update" modal at the user.

Response shape (all keys always present so the frontend can rely on it):

    {
      "version": "0.3.0",                 // currently running
      "latest_version": "0.3.28",         // from GitHub releases, or null
      "has_update": true,
      "release_url": "https://github.com/.../tag/v0.3.28",
      "release_published_at": "2026-06-02T17:43:56Z",
      "checked_at": "2026-06-03T07:50:00Z",
      "mac_app_install": false,           // PET-1117: BH-150 uninstall button
      "update_download_url": null         // PET-1117: BH-176 Windows installer
    }

`latest_version` may be null on a fresh boot before the first poll
completes, or if GitHub is unreachable. In both cases `has_update` is
false — the frontend should treat absence of confirmation as "no
update" rather than showing a stale or speculative banner.
"""

from fastapi import APIRouter, Request

from src.routes import system as _system

router = APIRouter()


@router.get("/version")
async def version(request: Request) -> dict:
    # PET-1117 — the facts of this installation travel with every answer: this
    # is the address the dashboard asks (see `system.install_facts`).
    return {**_version_snapshot(request), **_system.install_facts()}


def _version_snapshot(request: Request) -> dict:
    checker = getattr(request.app.state, "update_checker", None)
    if checker is None:
        # Update checking is best-effort — if it wasn't started (e.g.
        # aiohttp missing in some constrained env) we still answer with
        # the basic current-version envelope.
        cfg = getattr(request.app.state, "config", {})
        return {
            "version": cfg.get("version", "unknown"),
            "latest_version": None,
            "has_update": False,
            "release_url": None,
            "release_published_at": None,
            "checked_at": None,
        }
    return checker.snapshot()
