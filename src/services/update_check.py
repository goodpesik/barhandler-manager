"""Background poller that asks GitHub Releases for the latest published
release of barhandler-manager, caches the result, and exposes a tiny
sync API the routes layer reads on each /health or /version call.

Why a background poller:
- The dashboard polls /health every 2 s; the frontend pings the manager
  on a similar cadence to know if it's up. Hitting GitHub on every
  request would rate-limit us (60/hr unauthenticated) and add latency
  to a hot path.
- Once an hour is plenty — operators don't need second-level freshness.
- One check on startup so the first response already has the info.

Failure modes:
- GitHub unreachable / 5xx / 403 rate-limit → cached value (if any)
  stays; if no previous value, latest_version stays None and has_update
  is False (the frontend's "update available" banner stays hidden when
  we can't confirm anything).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional


_GH_RELEASES_URL = (
    "https://api.github.com/repos/goodpesik/barhandler-manager/releases/latest"
)
_POLL_INTERVAL_SECONDS = 60 * 60  # 1 hour
_REQUEST_TIMEOUT_SECONDS = 10

logger = logging.getLogger("update_check")


def _strip_v(tag: str) -> str:
    """`v0.3.28` → `0.3.28`. Idempotent."""
    return tag[1:] if tag.startswith("v") else tag


_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _parse_version(s: str) -> tuple[int, int, int]:
    """Minimal X.Y.Z parser — anything not matching returns (0,0,0) so
    a malformed local VERSION can't accidentally claim it's newer than
    GitHub. (We don't ship anything other than X.Y.Z tags.)"""
    m = _VERSION_RE.match(s)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


class UpdateChecker:
    """Periodic GitHub-releases poller. Drive lifecycle from FastAPI lifespan:

        checker = UpdateChecker(current_version="0.3.0")
        asyncio.create_task(checker.run_forever())
        ...
        await checker.stop()
    """

    def __init__(self, current_version: str) -> None:
        self.current_version = current_version
        self.latest_version: Optional[str] = None
        self.release_url: Optional[str] = None
        self.release_published_at: Optional[str] = None
        self.checked_at: Optional[str] = None
        self._stop = asyncio.Event()

    @property
    def has_update(self) -> bool:
        if not self.latest_version:
            return False
        return is_newer(self.latest_version, self.current_version)

    def snapshot(self) -> dict:
        """Return the cached release info as a dict ready to splice into
        /health or /version responses. All keys present even when we
        haven't checked yet so the frontend can rely on the shape."""
        return {
            "version": self.current_version,
            "latest_version": self.latest_version,
            "has_update": self.has_update,
            "release_url": self.release_url,
            "release_published_at": self.release_published_at,
            "checked_at": self.checked_at,
        }

    async def check_once(self) -> None:
        """One-shot GitHub poll. Updates internal state on success;
        leaves prior state untouched on any failure."""
        try:
            import aiohttp
        except ImportError:
            logger.debug("aiohttp not installed; update check skipped")
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    _GH_RELEASES_URL,
                    timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
                    headers={"Accept": "application/vnd.github+json"},
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "update check: GitHub returned %d", resp.status,
                        )
                        return
                    data = await resp.json()
        except Exception as e:
            logger.warning("update check failed: %r", e)
            return

        tag = data.get("tag_name") or ""
        if not tag:
            return
        self.latest_version = _strip_v(tag)
        self.release_url = data.get("html_url")
        self.release_published_at = data.get("published_at")
        self.checked_at = datetime.now(timezone.utc).isoformat()
        if self.has_update:
            logger.info(
                "update available: current=%s latest=%s url=%s",
                self.current_version, self.latest_version, self.release_url,
            )

    async def run_forever(self) -> None:
        """Initial check immediately, then sleep+check on the interval.
        Resilient to errors: every failure is logged, never raises."""
        try:
            await self.check_once()
        except Exception as e:
            logger.warning("update check: initial probe crashed: %r", e)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=_POLL_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self.check_once()
            except Exception as e:
                logger.warning("update check: periodic probe crashed: %r", e)

    async def stop(self) -> None:
        self._stop.set()
