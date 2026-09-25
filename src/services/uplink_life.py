"""PET-928 — remote diagnostics switch themselves off.

Remote diagnostics let support read this machine's logs and run commands on
it. That is a door, and a door left open is one nobody remembers opening: in
practice it is switched on for one problem and stays on for months.

So it has a life of one day, counted from when it was switched on and kept in
config.yaml — a restart must not hand the session another full day. The same
shutdown is reachable from the server, because the person who asked for it is
usually not the person sitting at the machine.

One function does the shutting, for all three ways in (the dashboard switch,
the day running out, an order from the server), so they cannot drift apart.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

#: How long remote diagnostics stay on without anybody touching them.
UPLINK_LIFETIME = timedelta(days=1)

#: How often the countdown is checked. A minute is far finer than a day needs
#: and costs nothing; it also bounds how long the door stays open past its
#: time if the process was asleep.
_CHECK_INTERVAL_SEC = 60


def enabled_at(cfg: dict) -> Optional[datetime]:
    """When the switch was flipped on, or None if it never was."""
    raw = ((cfg or {}).get("uplink") or {}).get("enabled_at") or ""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        # A hand-edited or truncated value must not keep the door open for
        # ever: treat it as unknown, which expires immediately below.
        log.warning("uplink enabled_at is not a date: %r", raw)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def expires_at(cfg: dict) -> Optional[datetime]:
    started = enabled_at(cfg)
    return None if started is None else started + UPLINK_LIFETIME


def is_expired(cfg: dict, now: Optional[datetime] = None) -> bool:
    """True when the session has outlived its day.

    An enabled uplink with NO start time counts as expired: that is either a
    config written before this existed or one somebody edited, and the safe
    reading of «we do not know when this was opened» is «close it».
    """
    uplink = (cfg or {}).get("uplink") or {}
    if not uplink.get("enabled"):
        return False
    due = expires_at(cfg)
    if due is None:
        return True
    return (now or datetime.now(timezone.utc)) >= due


async def shut_down(app_state: Any, cfg: dict, reason: str) -> bool:
    """Stop remote diagnostics and write that down. Returns False if already off.

    Deliberately tolerant: this runs from a timer and from a socket command,
    where there is nobody to show an error to. Whatever fails, the client is
    stopped — an open door matters more than a tidy config file.
    """
    uplink = (cfg or {}).setdefault("uplink", {})
    client = getattr(app_state, "uplink", None)
    if not uplink.get("enabled") and client is None:
        return False

    log.info("remote diagnostics off (%s)", reason)
    try:
        from src.services.log_uplink import set_active

        if client is not None:
            await client.stop()
            client.detach_handler_from_root()
        set_active(None)
        app_state.uplink = None
    except Exception as e:  # noqa: BLE001 — never let this leave the door open
        log.warning("stopping the uplink client failed: %s", e)
        app_state.uplink = None

    uplink["enabled"] = False
    uplink["enabled_at"] = ""
    try:
        from src.routes.system import persist_uplink_state

        persist_uplink_state(uplink)
    except Exception as e:  # noqa: BLE001
        # The config file is the memory of this across restarts; failing to
        # write it means the next boot may switch it on again, so it is worth
        # a loud line, but it is not worth keeping the socket open over.
        log.error("could not write the uplink state to config: %s", e)
    return True


async def watch(app_state: Any, cfg: dict) -> None:
    """Background loop: close the door when its day is up.

    Started at boot, so a manager that was restarted mid-session still counts
    from when the session began rather than from the restart.
    """
    while True:
        try:
            if is_expired(cfg):
                await shut_down(app_state, cfg, "a day has passed")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a watchdog must not die
            log.warning("uplink expiry check failed: %s", e)
        await asyncio.sleep(_CHECK_INTERVAL_SEC)
