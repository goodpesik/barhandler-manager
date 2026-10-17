"""PET-1087 — the app's device settings, kept on the station itself.

Which printer prints receipts, which one prints labels, where the manager
lives — all of it lived only in the browser's localStorage. Clear the
browser data, reinstall it, or sign in from another profile on the same
machine, and the till stands with its printers forgotten while the printers
themselves are registered and working right here.

The station already keeps what belongs to the station (`printers.json`,
`terminals.json`), so it keeps this too: the app saves its device settings
here on every change and takes them back when its own copy is empty.

Not the same thing as `offline_state.save_local_settings` (PET-973): that
snapshot exists only while offline mode is activated and is read by the
offline build. This one has no conditions at all — a shop that never turns
offline mode on still must not lose its printer roles.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("app-settings.json")

#: One shop's blob. Device settings are a handful of short strings plus the
#: printer list, so these bounds are wide; they are here to stop a bug (or a
#: caller that decided to park a photo here) from growing the file forever.
MAX_KEYS = 200
MAX_KEY_LEN = 200
MAX_VALUE_LEN = 64 * 1024
MAX_APPID_LEN = 100
#: A machine can serve two shops (a kiosk shared by a shop and its branch).
MAX_SHOPS = 10


class SettingsRefused(Exception):
    """The body is not device settings — the route answers 400."""


class StationSettings:
    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self._shops: dict[str, dict] = {}
        self._lock = threading.Lock()
        #: Which save came after which. Review found the first version
        #: evicting by `savedAt`, and a station clock that steps back then
        #: makes the blob just saved the oldest one — evicted inside the very
        #: call that reports success. A counter cannot step back.
        self._saves = 0

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("app-settings unreadable, starting empty: %s", exc)
            return
        shops = raw.get("shops") if isinstance(raw, dict) else None
        if not isinstance(shops, dict):
            logger.warning("app-settings has no shops, starting empty")
            return
        with self._lock:
            self._shops = {
                appid: blob
                for appid, blob in shops.items()
                if isinstance(appid, str) and isinstance(blob, dict) and isinstance(blob.get("items"), dict)
            }
            # The order of the file is the order of the saves it remembers.
            for order, blob in enumerate(self._shops.values(), start=1):
                blob.setdefault("order", order)
            self._saves = len(self._shops)
        logger.info("app-settings: device settings of %s shop(s)", len(self._shops))

    def _save_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"shops": self._shops}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def save(self, appid: str, items: object) -> dict:
        """Keep this shop's settings. Answers the blob's id and time, so the
        app can tell its own copy from a newer one saved elsewhere."""
        if appid is not None and not isinstance(appid, str):
            raise SettingsRefused("Некоректний заклад.")
        appid = (appid or "").strip()
        if not appid:
            raise SettingsRefused("Не вказано заклад.")
        if len(appid) > MAX_APPID_LEN:
            raise SettingsRefused("Некоректний заклад.")
        if not isinstance(items, dict) or not items:
            raise SettingsRefused("Немає налаштувань для збереження.")
        if len(items) > MAX_KEYS:
            raise SettingsRefused("Завелика кількість налаштувань.")
        clean: dict[str, str] = {}
        for key, value in items.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise SettingsRefused("Налаштування мусять бути рядками.")
            if len(key) > MAX_KEY_LEN:
                raise SettingsRefused("Назва налаштування завелика.")
            if len(value) > MAX_VALUE_LEN:
                raise SettingsRefused(f"Налаштування «{key}» завелике.")
            clean[key] = value
        with self._lock:
            self._saves += 1
            blob = {
                # The id, not the time, is what the app compares: a station's
                # clock can jump back, and then a newer blob would look older
                # than the copy already applied (as in offline settings).
                "id": uuid.uuid4().hex,
                "savedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "order": self._saves,
                "items": clean,
            }
            # Built apart and committed only after the file is written: an
            # unwritable disk must not leave the memory (and therefore GET)
            # ahead of what the station actually holds.
            next_shops = dict(self._shops)
            next_shops[appid] = blob
            if len(next_shops) > MAX_SHOPS:
                # The shop saved least recently goes — never the one being
                # saved right now.
                oldest = sorted(
                    (kv for kv in next_shops.items() if kv[0] != appid),
                    key=lambda kv: kv[1].get("order") or 0,
                )
                for appid_out, _ in oldest[: len(next_shops) - MAX_SHOPS]:
                    next_shops.pop(appid_out, None)
                    logger.info("app-settings: the settings of shop %s are forgotten (the file is full)", appid_out)
            kept, self._shops = self._shops, next_shops
            try:
                self._save_locked()
            except Exception:
                self._shops = kept
                raise
        logger.info("app-settings: %s settings saved for shop %s", len(clean), appid)
        return {"id": blob["id"], "savedAt": blob["savedAt"], "count": len(clean)}

    def load_shop(self, appid: str) -> Optional[dict]:
        appid = (appid or "").strip()
        if not appid:
            return None
        with self._lock:
            blob = self._shops.get(appid)
            return json.loads(json.dumps(blob)) if blob else None

    def shops(self) -> list[str]:
        with self._lock:
            return sorted(self._shops.keys())
