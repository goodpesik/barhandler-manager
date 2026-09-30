"""PET-972 — which shop is activated for offline mode on this machine.

Activation comes from Petshandler itself: only a signed-in admin can get a
device token from the server, so the online app takes one and hands it to the
manager (POST /offline/activate). The manager then:

* makes a fresh data key for the shop's local copy;
* keeps the token and the key with the operating system (offline_secrets);
* writes the rest of the config — no secrets — to
  APP_DIR/offline/<appid>/config.json, readable by this user only, through a
  temporary file and a rename;
* remembers which shop is active (APP_DIR/offline/active.json).

The supervisor (offline_service) reads `current_config()` every time it
starts the service, and restarts it when the config changes.

A developer can still point BHM_OFFLINE_CONFIG_FILE at a full JSON config.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets as pysecrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config import APP_DIR
from src.services import offline_secrets
from src.services.offline_service import DEFAULT_PRODUCT, PRODUCTS, runtime_dir

log = logging.getLogger(__name__)

#: A shop's appid and a device id: used in paths and in Keychain commands.
# fullmatch everywhere: `$` also matches before a trailing newline, and a
# newline inside a `security -i` line splits it into two commands.
_SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
#: The server the token was issued by: https, /api, and a host on the
#: product's own list (PRODUCTS[…].api_hosts).
_API_BASE = re.compile(r"https://([A-Za-z0-9.-]+)/api/?")


def offline_root() -> Path:
    return APP_DIR / "offline"


def shop_folder(appid: str) -> Path:
    return offline_root() / appid


class ActivationError(ValueError):
    """What was sent cannot activate offline mode; the text is for a person."""


def _write_json(path: Path, value: dict) -> None:
    offline_secrets._write_private(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def active_appid() -> Optional[str]:
    try:
        raw = json.loads((offline_root() / "active.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    appid = raw.get("appid") if isinstance(raw, dict) else None
    return appid if isinstance(appid, str) and _SAFE_ID.fullmatch(appid) else None


def activate(payload: dict) -> dict:
    """Store what the online app sent; returns the public part for the answer."""
    # Which product's till this is (only Petshandler so far, see PRODUCTS).
    product = str(payload.get("product") or DEFAULT_PRODUCT)
    if product not in PRODUCTS:
        raise ActivationError("Офлайн-режим для цього продукту не підтримується.")
    appid = str(payload.get("appid") or "")
    device_id = str(payload.get("deviceId") or "")
    token = str(payload.get("deviceToken") or "")
    api_base = str(payload.get("apiBase") or "")
    if not _SAFE_ID.fullmatch(appid):
        raise ActivationError("Некоректний ідентифікатор закладу.")
    if not _SAFE_ID.fullmatch(device_id):
        raise ActivationError("Некоректний ідентифікатор пристрою.")
    if not token.startswith("pho_"):
        raise ActivationError("Некоректний токен пристрою.")
    api = _API_BASE.fullmatch(api_base)
    if not api or api.group(1).lower() not in PRODUCTS[product].api_hosts:
        raise ActivationError("Некоректна адреса сервера.")

    folder = shop_folder(appid)
    previous = active_appid()
    if previous and previous != appid:
        # One till, one shop: another shop's data may still hold operations
        # that have not reached its server. Refusing is safer than guessing.
        raise ActivationError(
            "Офлайн-режим уже увімкнено для іншого закладу. Спершу вимкніть його там.",
        )
    old = offline_secrets.load(appid, folder) if previous == appid else None
    # A re-activation (a new token) keeps the data key: the local copy was
    # encrypted with it, and a new key would make it unreadable.
    data_key = (old or {}).get("dataKey") or base64.b64encode(pysecrets.token_bytes(32)).decode("ascii")
    offline_secrets.save(appid, {"deviceToken": token, "dataKey": data_key}, folder)
    config = {
        "product": product,
        "appid": appid,
        "apiBase": api_base.rstrip("/"),
        "deviceId": device_id,
        "shopName": str(payload.get("shopName") or "")[:200],
        "tokenExpiresAt": payload.get("expiresAt"),
        "activatedAt": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(folder / "config.json", config)
    _write_json(offline_root() / "active.json", {"appid": appid})
    log.info(
        "offline mode activated for shop %s, device %s (%s)",
        appid, device_id, "new token" if previous == appid else "first activation",
    )
    return {k: config[k] for k in ("product", "appid", "deviceId", "shopName", "tokenExpiresAt", "activatedAt")}


def check_can_deactivate(queued: Optional[int]) -> None:
    """Refuse while operations made offline have not reached the server."""
    if queued:
        raise ActivationError(
            f"Офлайн-операцій, що ще не надійшли на сервер: {queued}. "
            "Вимкнення можливе після синхронізації.",
        )
    if queued is None:
        raise ActivationError(
            "Не вдалося перевірити чергу офлайн-операцій. "
            "Вимкнення можливе, коли офлайн-сервіс працює.",
        )


def forget() -> Optional[str]:
    """Switch offline mode off: the secrets and the active mark go. Returns the shop."""
    appid = active_appid()
    if not appid:
        return None
    offline_secrets.delete(appid, shop_folder(appid))
    try:
        (offline_root() / "active.json").unlink()
    except FileNotFoundError:
        pass
    log.info("offline mode switched off for shop %s", appid)
    return appid


def remove_data(appid: str) -> None:
    """The shop's local copy, once the service that held it has stopped."""
    folder = shop_folder(appid)
    shutil.rmtree(folder, ignore_errors=True)
    if folder.exists():
        log.error("offline data of shop %s could not be removed completely: %s", appid, folder)
    else:
        log.info("offline data of shop %s removed", appid)


def current_config() -> Optional[dict]:
    """The full config the service is started with, or None when not activated."""
    path = os.environ.get("BHM_OFFLINE_CONFIG_FILE")
    if path:
        try:
            cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("offline config %s unreadable: %s", path, e)
            return None
        return cfg if isinstance(cfg, dict) else None

    appid = active_appid()
    if not appid:
        return None
    folder = shop_folder(appid)
    try:
        stored = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("offline config for shop %s unreadable: %s", appid, e)
        return None
    kept = offline_secrets.load(appid, folder)
    if not kept or not kept.get("deviceToken") or not kept.get("dataKey"):
        log.warning("offline secrets for shop %s are missing: activate again from Petshandler", appid)
        return None
    return {
        "appid": appid,
        "apiBase": stored["apiBase"],
        "deviceId": stored["deviceId"],
        "deviceToken": kept["deviceToken"],
        "dataKey": kept["dataKey"],
        "dataDir": str(folder / "data"),
        "staticDir": str(runtime_dir(stored.get("product") or DEFAULT_PRODUCT) / "app"),
    }


def public_status() -> dict:
    """What the manager shows about activation; never the secrets."""
    appid = active_appid()
    if not appid:
        return {"activated": False}
    try:
        stored = json.loads((shop_folder(appid) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    return {
        "activated": True,
        "product": stored.get("product") or DEFAULT_PRODUCT,
        "appid": appid,
        "shopName": stored.get("shopName") or "",
        "deviceId": stored.get("deviceId"),
        "tokenExpiresAt": stored.get("tokenExpiresAt"),
        "activatedAt": stored.get("activatedAt"),
    }


# ---- PET-973: the online app's device settings, for the offline build ----

#: A snapshot is a handful of short preferences; anything bigger is not one.
LOCAL_SETTINGS_MAX_BYTES = 64 * 1024
_SETTING_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,100}")


def save_local_settings(appid: str, items: object) -> dict:
    """Keep what the online app sent for the offline build of the same shop.

    Which keys go is decided by the app (an allow-list there: device and
    printer settings, language, list views — never the session, the tenant or
    temporary state). Here only the shape is checked.
    """
    active = active_appid()
    if not active:
        raise ActivationError("Офлайн-режим на цьому компʼютері не увімкнено.")
    if appid != active:
        raise ActivationError("Офлайн-режим на цьому компʼютері увімкнено для іншого закладу.")
    if not isinstance(items, dict) or not all(
        isinstance(k, str) and _SETTING_KEY.fullmatch(k) and isinstance(v, str) for k, v in items.items()
    ):
        raise ActivationError("Некоректні налаштування.")
    body = {"savedAt": datetime.now(timezone.utc).isoformat(), "items": items}
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if len(raw) > LOCAL_SETTINGS_MAX_BYTES:
        raise ActivationError("Налаштування завеликі.")
    offline_secrets._write_private(shop_folder(appid) / "local-settings.json", raw)
    log.info("offline: %d browser settings kept for shop %s", len(items), appid)
    return {"savedAt": body["savedAt"], "count": len(items)}


def load_local_settings() -> Optional[dict]:
    """The active shop's snapshot: {savedAt, items}, or None."""
    appid = active_appid()
    if not appid:
        return None
    try:
        body = json.loads((shop_folder(appid) / "local-settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) and isinstance(body.get("items"), dict) else None
