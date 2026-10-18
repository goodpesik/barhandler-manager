"""Whether the client app offers offline mode at all.

Offline mode is switched off on production in Petshandler for now: the app
hides everything about it there. The manager's dashboard showed its «Offline
mode» button whenever this build carries the offline runtime, so a shop on
production saw a button for a mode its own app does not offer.

The app says it with every request to the manager, in the
`X-Offline-Offered` header (`1` or `0`). The last word is kept on disk, so a
restarted manager does not show the button before the app has spoken again.
A manager that has never heard from an app offering it keeps it hidden: an
older app, and the apps of the other products, send nothing at all.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

HEADER = "x-offline-offered"


def parse_header(raw: Optional[str]) -> Optional[bool]:
    """`1` / `0` (also `true` / `false`); anything else is no word at all."""
    v = (raw or "").strip().lower()
    if v in ("1", "true"):
        return True
    if v in ("0", "false"):
        return False
    return None


class OfflineOffer:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._offered: Optional[bool] = None

    def load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            value = data.get("offered")
            self._offered = value if isinstance(value, bool) else None
        except FileNotFoundError:
            self._offered = None
        except (OSError, ValueError) as e:
            # A broken file is the same as no word: the button stays hidden.
            logger.warning("offline offer file unreadable (%s): %s", self._path, e)
            self._offered = None

    def offered(self) -> bool:
        return self._offered is True

    def note(self, headers: Mapping[str, str]) -> None:
        """Remember what the app said in this request, if it said anything."""
        word = parse_header(headers.get(HEADER))
        if word is None:
            return
        with self._lock:
            if word == self._offered:
                return
            self._offered = word
            # Said out loud: this is what decides whether the dashboard shows
            # offline mode, and nothing else would tell why it appeared.
            logger.info("client app %s offline mode", "offers" if word else "does not offer")
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps({
                        "offered": word,
                        "at": datetime.now(timezone.utc).isoformat(),
                    }),
                    encoding="utf-8",
                )
                os.replace(tmp, self._path)
            except OSError as e:
                # Kept in memory anyway; only a restart would forget it.
                logger.warning("offline offer not saved (%s): %s", self._path, e)
