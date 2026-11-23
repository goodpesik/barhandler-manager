"""PET-972 — which shop is activated for offline mode on this machine.

Activation comes from Petshandler itself: only a signed-in admin can get a
device token from the server, so the online app takes one and hands it to the
manager (POST /offline/activate). The manager then:

* makes a fresh data key for the shop's local copy;
* keeps the token and the key in a private file (offline_secrets);
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
import hashlib
import json
import logging
import os
import re
import secrets as pysecrets
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

from src.config import APP_DIR
from src.services import offline_secrets
from src.services.offline_service import DEFAULT_PRODUCT, PRODUCTS, runtime_dir

log = logging.getLogger(__name__)

#: A shop's appid and a device id: used in paths.
# fullmatch everywhere: `$` also matches before a trailing newline.
_SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
#: The highest till number a shop's computers may take (FC-798).
MAX_DEVICE_NUMBER = 99


def _api_base_pattern(product: str) -> "re.Pattern[str]":
    """The server the token was issued by: https, the product's own path
    (/api for Petshandler) and a host on its list (PRODUCTS[…].api_hosts)."""
    return re.compile(r"https://([A-Za-z0-9.-]+)" + re.escape(PRODUCTS[product].api_path) + r"/?")


def offline_root(product: str = DEFAULT_PRODUCT) -> Path:
    """FC-798 — each product's shops in a folder of its own: two tills of
    different products on one computer never share a marker or a copy."""
    return APP_DIR / PRODUCTS[product].data_folder


def shop_folder(appid: str, product: str = DEFAULT_PRODUCT) -> Path:
    return offline_root(product) / appid


class ActivationError(ValueError):
    """What was sent cannot activate offline mode; the text is for a person."""


def _write_json(path: Path, value: dict) -> None:
    offline_secrets._write_private(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def active_appid(product: str = DEFAULT_PRODUCT) -> Optional[str]:
    try:
        raw = json.loads((offline_root(product) / "active.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    appid = raw.get("appid") if isinstance(raw, dict) else None
    return appid if isinstance(appid, str) and _SAFE_ID.fullmatch(appid) else None


def activate(payload: dict) -> dict:
    """Store what the online app sent; returns the public part for the answer."""
    # Which product's till this is (see PRODUCTS); Petshandler when not said.
    product = str(payload.get("product") or DEFAULT_PRODUCT)
    if product not in PRODUCTS:
        raise ActivationError("Офлайн-режим для цього продукту не підтримується.")
    spec = PRODUCTS[product]
    appid = str(payload.get("appid") or "")
    device_id = str(payload.get("deviceId") or "")
    token = str(payload.get("deviceToken") or "")
    api_base = str(payload.get("apiBase") or "")
    if not _SAFE_ID.fullmatch(appid):
        raise ActivationError("Некоректний ідентифікатор закладу.")
    if not _SAFE_ID.fullmatch(device_id):
        raise ActivationError("Некоректний ідентифікатор пристрою.")
    if not token.startswith(spec.token_prefix):
        raise ActivationError("Некоректний токен пристрою.")
    api = _api_base_pattern(product).fullmatch(api_base)
    if not api or api.group(1).lower() not in spec.api_hosts:
        raise ActivationError("Некоректна адреса сервера.")
    device_number = _device_number(payload) if spec.numbered_devices else None

    folder = shop_folder(appid, product)
    previous = active_appid(product)
    if previous and previous != appid:
        # One till, one shop: another shop's data may still hold operations
        # that have not reached its server. Refusing is safer than guessing.
        raise ActivationError(
            "Офлайн-режим уже увімкнено для іншого закладу. Спершу вимкніть його там.",
        )
    # Read even when active.json was lost: the shop's own secrets file decides
    # whether its data key still exists. A file that cannot be read now raises
    # (SecretsUnreadable): the activation fails and nothing is moved aside.
    old = offline_secrets.load(appid, folder)
    # A re-activation (a new token) keeps the data key: the local copy was
    # encrypted with it, and a new key would make it unreadable.
    data_key = (old or {}).get("dataKey")
    if not data_key:
        data_key = base64.b64encode(pysecrets.token_bytes(32)).decode("ascii")
        # A copy left from before (a switch-off that could not remove it all)
        # was encrypted with a key that is gone: moved aside, never opened
        # with the new one.
        # Such folders are kept (not deleted: the lock that stopped the
        # removal may still hold) and go with the shop's next full removal.
        stale = folder / "data"
        aside = folder / f"data.unreadable-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}"
        try:
            stale.rename(aside)
            log.warning("offline: shop %s had a local copy without its key, moved to %s", appid, aside.name)
        except FileNotFoundError:
            pass  # nothing left over — or a request sent twice already moved it
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
    public_keys = ["product", "appid", "deviceId", "shopName", "tokenExpiresAt", "activatedAt"]
    if device_number is not None:
        config["deviceNumber"] = device_number
        public_keys.append("deviceNumber")
    _write_json(folder / "config.json", config)
    _write_json(offline_root(product) / "active.json", {"appid": appid})
    how = "new token" if previous == appid else "first activation"
    if product == "petshandler":
        # Petshandler keeps its original line: logs are searched by it.
        log.info("offline mode activated for shop %s, device %s (%s)", appid, device_id, how)
    else:
        log.info(
            "offline mode activated for %s shop %s, device %s number %s (%s)",
            product, appid, device_id, device_number, how,
        )
    return {k: config[k] for k in public_keys}


def _device_number(payload: dict) -> int:
    """FC-798 — the till's number among the shop's computers (1 when not
    sent): its receipts are numbered D<n>-0001, so two tills of one shop never
    print the same number. Chosen by the person who switches the till on."""
    raw = payload.get("deviceNumber")
    if raw is None:
        return 1
    if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= MAX_DEVICE_NUMBER:
        return raw
    raise ActivationError(f"Номер каси має бути цілим числом від 1 до {MAX_DEVICE_NUMBER}.")


class QueueOnDisk(NamedTuple):
    """What the local copy says it still has to send.

    `unreadable` is why the copy could not be read, and it is a THIRD answer:
    neither a count of waiting operations nor «nothing waiting». `count` is
    None beside it, and the caller decides what to do with a dependency it
    cannot read — which is never «refused for good», see `check_can_deactivate`.
    """

    count: Optional[int]
    unreadable: Optional[str] = None


def queue_on_disk(
    appid: str, timeout: float = 5, quiet: bool = False, product: str = DEFAULT_PRODUCT,
) -> QueueOnDisk:
    """Operations still waiting for the server, read from the local copy itself.

    For when the service is not running and cannot be asked — without its
    secrets it never starts, and switching off must not depend on it. The
    queue's status column is plain text (only the payload is sealed), so no
    key is needed. A copy that is there but cannot be read comes back
    `unreadable` with the reason, not as a count.

    `quiet` leaves out the routine lines for callers that poll (the update
    guard asks every few seconds); a copy that cannot be read is always logged.
    """
    path = shop_folder(appid, product) / "data" / "offline.sqlite"
    try:
        if not path.exists():
            if not quiet:
                log.info("offline: shop %s has no local copy, nothing queued", appid)
            return QueueOnDisk(0)
        con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=timeout)
        try:
            (n,) = con.execute("SELECT count(*) FROM outbox WHERE status = 'queued'").fetchone()
        finally:
            con.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            if not quiet:
                log.info("offline: shop %s local copy has no queue yet", appid)
            return QueueOnDisk(0)
        log.warning("offline: queue of shop %s unreadable on disk: %s", appid, e)
        return QueueOnDisk(None, str(e))
    except (sqlite3.DatabaseError, OSError, ValueError) as e:
        log.warning("offline: queue of shop %s unreadable on disk: %s", appid, e)
        return QueueOnDisk(None, str(e))
    if not quiet or n:
        log.info("offline: shop %s queue read from disk: %s waiting", appid, n)
    return QueueOnDisk(int(n))


def queued_on_disk(
    appid: str, timeout: float = 5, quiet: bool = False, product: str = DEFAULT_PRODUCT,
) -> Optional[int]:
    """The count alone, for callers that have nothing to say about the reason.

    None keeps meaning «the copy is there and cannot be read»; `queue_on_disk`
    carries that reason for callers that put it in their own log line.
    """
    return queue_on_disk(appid, timeout=timeout, quiet=quiet, product=product).count


class FiscalOwed(NamedTuple):
    """What the local copy says it still owes the tax service.

    `unjudged` is why the copy could not be judged, and it is a THIRD answer:
    neither «nothing owed» nor «something owed». The counts are None beside
    it, and the caller decides what to do with a dependency it cannot read —
    which is never «refused for good», see `check_can_deactivate`.

    `documents` is how many fiscal documents the copy holds while the count of
    unsent ones cannot be read. It is owed, not unjudgeable: the rows
    themselves say there is something to lose, and `pending` is None beside
    them because how many of them are unsent is exactly what is missing.
    """

    pending: Optional[int]
    sessions: int
    unjudged: Optional[str] = None
    documents: int = 0


def fiscal_owed_on_disk(appid: str, product: str = DEFAULT_PRODUCT) -> FiscalOwed:
    """What the local copy still owes the tax service: unsent documents, sessions.

    For when the service cannot be asked. Whether a document was sent is
    inside its sealed row, so the service keeps the count of unsent ones in
    the clear (meta `fiscalPending`, PET-1120); an open session is a meta row
    of its own, whose key says so without the payload.

    A copy that nothing can be read out of — a damaged file, a schema without
    these names — comes back `unjudged` with the reason, not as a count.
    Fiscal documents with no count of unsent ones beside them are a different
    answer: the rows are readable and they are owed, see `FiscalOwed`. The
    same rule for every product, whatever build the till came from.

    A product without registers of its own (PRODUCTS[…].fiscal) owes the tax
    service nothing from this computer.
    """
    if not PRODUCTS[product].fiscal:
        return FiscalOwed(0, 0)
    path = shop_folder(appid, product) / "data" / "offline.sqlite"
    try:
        if not path.exists():
            return FiscalOwed(0, 0)
        con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'fiscalPending'").fetchone()
            (sessions,) = con.execute(
                "SELECT count(*) FROM meta WHERE key LIKE 'fiscalOfflineSession:%'"
            ).fetchone()
            if row is not None:
                pending = int(row[0])
            else:
                (docs,) = con.execute("SELECT count(*) FROM entity WHERE kind = 'fiscalDocument'").fetchone()
                if docs:
                    # A build that signs documents without keeping the count of
                    # unsent ones in the clear. Nothing here says they reached
                    # the tax service, and the rows go with the local copy.
                    log.warning(
                        "offline: shop %s holds %s fiscal documents and no count of unsent ones: "
                        "nothing on disk says they reached the tax service",
                        appid, docs,
                    )
                    return FiscalOwed(None, int(sessions), None, int(docs))
                pending = 0
        finally:
            con.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return FiscalOwed(0, 0)
        log.warning("offline: fiscal state of shop %s cannot be judged: %s", appid, e)
        return FiscalOwed(None, 0, str(e))
    except (sqlite3.DatabaseError, OSError, ValueError) as e:
        log.warning("offline: fiscal state of shop %s cannot be judged: %s", appid, e)
        return FiscalOwed(None, 0, str(e))
    log.info("offline: shop %s fiscal state read from disk: %s unsent, %s sessions", appid, pending, sessions)
    return FiscalOwed(pending, int(sessions))


def check_can_deactivate(
    queued: Optional[int],
    fiscal_pending: Optional[int] = 0,
    fiscal_sessions: int = 0,
    fiscal_documents: int = 0,
) -> None:
    """Refuse while anything made offline has not reached the server or the tax service.

    Switching off removes the local copy. A fiscal document signed offline and
    not yet handed to the tax service would be lost with it, and its fiscal
    number never arrives, which is what gets a registrar blocked; an open
    offline session has to be brought back online the same way.

    WHAT IS OWED IS A REFUSAL; WHAT CANNOT BE READ AT ALL IS NOT.
    The same split for every dependency this guard has. Each count is None
    when that part of the copy could not be read (`QueueOnDisk.unreadable`,
    `FiscalOwed.unjudged`). That used to be refused with «restart the manager
    and try again», which is an answer no one can act on: a till whose service
    never comes up again has nothing but the disk to be judged by, and no
    restart makes that copy say more. The refusal stays for the thing it
    protects — operations waiting for the server, documents the tax service
    has not got, an open session — and a part that cannot be read lets the
    switch-off through, with the reason in the log of whoever read it.

    `fiscal_documents` is the one count that stands without a number of unsent
    ones beside it: document rows on disk and no count of them in the clear
    means nothing says they were sent, so they are owed.
    """
    if queued:
        raise ActivationError(
            f"Офлайн-операцій, що ще не надійшли на сервер: {queued}. "
            "Вимкнення можливе після синхронізації.",
        )
    if fiscal_sessions:
        raise ActivationError(
            "Офлайн-каса ще працює в офлайн-сесії. "
            "Вимкнення можливе після повернення каси в онлайн.",
        )
    if fiscal_pending:
        raise ActivationError(
            f"Фіскальних документів, ще не переданих до ДПС: {fiscal_pending}. "
            "Вимкнення можливе після їх передачі.",
        )
    if fiscal_documents:
        raise ActivationError(
            f"Фіскальних документів на цьому комп'ютері: {fiscal_documents}, "
            "і немає даних про їх передачу до ДПС. Вимкнення можливе після "
            "повернення каси в онлайн.",
        )


def forget(product: str = DEFAULT_PRODUCT) -> Optional[str]:
    """Switch offline mode off: the secrets and the active mark go. Returns the shop."""
    appid = active_appid(product)
    if not appid:
        return None
    offline_secrets.delete(appid, shop_folder(appid, product))
    try:
        (offline_root(product) / "active.json").unlink()
    except FileNotFoundError:
        pass
    log.info("offline mode switched off for shop %s", appid)
    return appid


def remove_data(appid: str, product: str = DEFAULT_PRODUCT) -> None:
    """The shop's local copy, once the service that held it has stopped."""
    folder = shop_folder(appid, product)
    shutil.rmtree(folder, ignore_errors=True)
    if folder.exists():
        log.error("offline data of shop %s could not be removed completely: %s", appid, folder)
    else:
        log.info("offline data of shop %s removed", appid)


def current_config(product: str = DEFAULT_PRODUCT) -> Optional[dict]:
    """The full config the service is started with, or None when not activated."""
    # A developer's full config: BHM_OFFLINE_CONFIG_FILE for Petshandler,
    # BHM_OFFLINE_CONFIG_FILE_<PRODUCT> for the others.
    env = "BHM_OFFLINE_CONFIG_FILE" if product == DEFAULT_PRODUCT else f"BHM_OFFLINE_CONFIG_FILE_{product.upper()}"
    path = os.environ.get(env)
    if path:
        try:
            cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("offline config %s unreadable: %s", path, e)
            return None
        return cfg if isinstance(cfg, dict) else None

    appid = active_appid(product)
    if not appid:
        return None
    folder = shop_folder(appid, product)
    try:
        stored = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("offline config for shop %s unreadable: %s", appid, e)
        return None
    # A config without what the service needs is not activated — returned as
    # None, not raised: an exception here would end the supervisor.
    if not isinstance(stored, dict) or not stored.get("apiBase") or not stored.get("deviceId"):
        log.warning("offline config for shop %s is incomplete: activate again from Petshandler", appid)
        return None
    # SecretsUnreadable goes up: «cannot read it now» is not «not activated»,
    # and the supervisor keeps a running service on it.
    kept = offline_secrets.load(appid, folder)
    if not kept or not kept.get("deviceToken") or not kept.get("dataKey"):
        log.warning("offline secrets for shop %s are missing: activate again from Petshandler", appid)
        return None
    cfg = {
        "appid": appid,
        "apiBase": stored["apiBase"],
        "deviceId": stored["deviceId"],
        "deviceToken": kept["deviceToken"],
        "dataKey": kept["dataKey"],
        "dataDir": str(folder / "data"),
        "staticDir": str(runtime_dir(stored.get("product") or product) / "app"),
    }
    if PRODUCTS[product].numbered_devices:
        cfg["deviceNumber"] = _stored_device_number(stored)
    return cfg


def _stored_device_number(stored: dict) -> int:
    """The number kept at activation; 1 for a config written without one."""
    n = stored.get("deviceNumber")
    return n if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= MAX_DEVICE_NUMBER else 1


def public_status(product: str = DEFAULT_PRODUCT) -> dict:
    """What the manager shows about activation; never the secrets."""
    appid = active_appid(product)
    if not appid:
        return {"activated": False}
    try:
        stored = json.loads((shop_folder(appid, product) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    status = {
        "activated": True,
        "product": stored.get("product") or product,
        "appid": appid,
        "shopName": stored.get("shopName") or "",
        "deviceId": stored.get("deviceId"),
        "tokenExpiresAt": stored.get("tokenExpiresAt"),
        "activatedAt": stored.get("activatedAt"),
    }
    if PRODUCTS[product].numbered_devices:
        status["deviceNumber"] = _stored_device_number(stored)
    return status


# ---- PET-973: the online app's device settings, for the offline build ----

#: A snapshot is a handful of short preferences; anything bigger is not one.
LOCAL_SETTINGS_MAX_BYTES = 64 * 1024
_SETTING_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
#: The manager's own address may only be this computer: the offline build
#: sends every print and status call there (the app checks it again).
_LOCAL_URL = re.compile(r"https?://(localhost|127\.0\.0\.1)(:\d{1,5})?/?")
_VALUE_CHECKS = {"phm.manager.url": _LOCAL_URL}


def save_local_settings(appid: str, items: object, product: str = DEFAULT_PRODUCT) -> dict:
    """Keep what the online app sent for the offline build of the same shop.

    Which keys go is decided by the app (an allow-list there: device and
    printer settings, language, list views — never the session, the tenant or
    temporary state). Here only the shape is checked.
    """
    active = active_appid(product)
    if not active:
        raise ActivationError("Офлайн-режим на цьому компʼютері не увімкнено.")
    if appid != active:
        raise ActivationError("Офлайн-режим на цьому компʼютері увімкнено для іншого закладу.")
    if not isinstance(items, dict) or not all(
        isinstance(k, str) and _SETTING_KEY.fullmatch(k) and isinstance(v, str) for k, v in items.items()
    ):
        raise ActivationError("Некоректні налаштування.")
    for key, check in _VALUE_CHECKS.items():
        if key in items and not check.fullmatch(items[key]):
            raise ActivationError("Некоректні налаштування.")
    # A new id when the settings changed: the offline build applies a snapshot
    # it has not applied yet, not «a later time» (the till's clock can jump
    # back). The same settings again keep their id, so an online reload does
    # not undo what was changed offline.
    previous = load_local_settings(product)
    same = previous is not None and previous.get("items") == items and isinstance(previous.get("id"), str)
    body = {
        "id": previous["id"] if same else uuid.uuid4().hex,
        "savedAt": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if len(raw) > LOCAL_SETTINGS_MAX_BYTES:
        raise ActivationError("Налаштування завеликі.")
    offline_secrets._write_private(shop_folder(appid, product) / "local-settings.json", raw)
    log.info("offline: %d browser settings kept for %s shop %s", len(items), product, appid)
    return {"id": body["id"], "savedAt": body["savedAt"], "count": len(items)}


#: A key container is a few kilobytes; anything beyond this is not one.
FISCAL_KEY_MAX_BYTES = 1024 * 1024
#: What the upload request itself may weigh: the base64 of a container at the
#: cap (four characters carry three bytes), and room for the rest of the JSON
#: object around it. The route refuses a bigger body while it is still
#: arriving, so the cap holds for what is buffered and not only for what is
#: decoded.
FISCAL_KEY_MAX_REQUEST_BYTES = (FISCAL_KEY_MAX_BYTES // 3 + 1) * 4 + 4 + 8192
#: What a file dropped into the fiscal-keys folder may be called.
_KEY_FILE_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")

#: THE FILE KINDS THIS FISCAL STACK READS, NAMED ONE BY ONE.
#:
#: Key containers: `Key-6.dat` (IIT), `.pfx`/`.p12`, a PrivatBank `.jks`,
#: IIT's older `.zs2`, a PBES2 container in PEM (`.pem`, `.key`) or DER
#: (`.pk8`). Certificates kept beside a container: `.cer`, `.crt`, `.der`.
#: An allow-list and not «anything but an executable»: a person who picks
#: the wrong file in the browser is told so here, rather than on the first
#: receipt of a day without internet.
FISCAL_KEY_SUFFIXES: tuple[str, ...] = (
    ".dat", ".pfx", ".p12", ".jks", ".zs2", ".pem", ".key", ".pk8",
    ".cer", ".crt", ".der",
)


def fiscal_keys_folder(appid: str, product: str = DEFAULT_PRODUCT) -> Path:
    """Where the key containers of a shop's offline registers are kept.

    OUTSIDE the shop's own folder, and that is the whole point: switching
    offline mode off removes the local copy and everything beside it
    (`remove_data`), while a key container has to outlive it — a shop that
    switches offline mode off and on again would otherwise be left with
    registers whose settings name a file that is no longer there, and nothing
    to sign a receipt with in the first shop without internet (found by the
    second review round).

    Inside the product's own folder (`offline_root`), like every other file
    of its till: two products' tills on one computer never share a container,
    and a shop and a studio with the same appid cannot meet.
    """
    return offline_root(product) / "fiscal-keys" / appid


def _safe_key_name(file_name: str) -> str:
    """The name the file is kept under: its own, with nothing of a path in it.

    The browser sends whatever the person picked, and a name is not a path:
    `..`, a slash or a backslash would put the container anywhere on the disk.
    Everything outside the allowed set becomes an underscore, and a name left
    with nothing is refused instead of invented.

    The extension is kept when the name has to be cut: a name of seventy
    characters would otherwise lose its `.dat` and stop looking like what it is.
    """
    bare = str(file_name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", bare).lstrip(".")
    if len(cleaned) > 48:
        stem, dot, ext = cleaned.rpartition(".")
        cleaned = (stem[: 48 - len(ext) - 1] + dot + ext) if dot and len(ext) <= 8 else cleaned[:48]
    if not cleaned or not _KEY_FILE_NAME.fullmatch(cleaned):
        raise ActivationError("Некоректна назва файла ключа.")
    return cleaned


def _check_key_suffix(name: str, appid: str, product: str) -> None:
    """Refuse a file whose kind this fiscal stack does not read.

    Before the content is decoded and long before anything is written: a file
    that cannot be a container or a certificate has no business in the folder
    that holds the right to sign fiscal documents. The reason goes to the log
    in full; the person is told which kinds are taken.
    """
    suffix = name[name.rfind(".") :].lower() if "." in name else ""
    if suffix not in FISCAL_KEY_SUFFIXES:
        log.warning(
            "offline: fiscal key refused for %s shop %s: file kind %r is not read by the fiscal stack",
            product, appid, suffix or name,
        )
        raise ActivationError(
            "Цей тип файла не підходить для ключа. "
            "Підходять контейнери ключа (.dat, .pfx, .p12, .jks, .zs2, .pem, .key, .pk8) "
            "та сертифікати (.cer, .crt, .der).",
        )


def save_fiscal_key(
    appid: str, file_name: str, content_base64: str, product: str = DEFAULT_PRODUCT,
) -> dict:
    """PET-1088 — keep an uploaded key container (or its certificate) on this machine.

    The offline service signs with a file that lives on the shop's own
    computer, so the person uploads it here instead of typing a path they have
    to find in a file manager. The manager is the one that keeps it: its folder
    outlives the service's data, which goes when offline mode is switched off.

    The file is written like every other private file of ours — through a
    temporary file and a rename, readable by this user only — and the answer is
    the path the offline register settings then carry.

    FC-844 — for the till of the product named, Petshandler when not said: a
    studio's container goes to the FitStudioCRM folder and is checked against
    the FitStudioCRM activation.
    """
    active = active_appid(product)
    if not active:
        log.warning("offline: fiscal key refused: %s offline mode is not on here", product)
        raise ActivationError("Офлайн-режим на цьому компʼютері не увімкнено.")
    if appid != active:
        log.warning("offline: fiscal key refused: %s offline mode here is for another shop", product)
        raise ActivationError("Офлайн-режим на цьому компʼютері увімкнено для іншого закладу.")
    name = _safe_key_name(file_name)
    _check_key_suffix(name, appid, product)
    raw = str(content_base64 or "")
    # Four base64 characters carry three bytes: a body too big to be a
    # container is refused before it is decoded into memory.
    if len(raw) > (FISCAL_KEY_MAX_BYTES // 3 + 1) * 4 + 4:
        log.warning("offline: fiscal key refused for %s shop %s: body over the cap", product, appid)
        raise ActivationError("Файл завеликий для ключа.")
    try:
        data = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        log.warning("offline: fiscal key refused for %s shop %s: the body is not base64", product, appid)
        raise ActivationError("Файл не вдалося прочитати.")
    if not data:
        log.warning("offline: fiscal key refused for %s shop %s: the file is empty", product, appid)
        raise ActivationError("Файл порожній.")
    if len(data) > FISCAL_KEY_MAX_BYTES:
        log.warning(
            "offline: fiscal key refused for %s shop %s: %d bytes is over the cap of %d",
            product, appid, len(data), FISCAL_KEY_MAX_BYTES,
        )
        raise ActivationError("Файл завеликий для ключа.")
    """THE CONTENT NAMES THE FILE, NOT THE NAME THE BROWSER SENT.

    Every shop's container is called `Key-6.dat`, so two registers of one shop
    upload two different keys under one name — and the second silently replaced
    the first, leaving register A signing with register B's key (found by the
    second review round). The digest of the content goes in front of the name:
    two different containers cannot meet, and the same one uploaded twice lands
    on itself, which costs nothing.
    """
    digest = hashlib.sha256(data).hexdigest()[:16]
    name = f"{digest}-{name}"
    path = fiscal_keys_folder(appid, product) / name
    offline_secrets._write_private(path, data)
    # The name and the path are in the log and the content never is: a key
    # container is the right to sign fiscal documents. Neither is the password:
    # it never travels through this route at all.
    log.info(
        "offline: fiscal key file %s kept for %s shop %s (%d bytes) at %s",
        name, product, appid, len(data), path,
    )
    return {"path": str(path), "name": name, "bytes": len(data)}


def load_local_settings(product: str = DEFAULT_PRODUCT) -> Optional[dict]:
    """The active shop's snapshot: {savedAt, items}, or None."""
    appid = active_appid(product)
    if not appid:
        return None
    try:
        body = json.loads((shop_folder(appid, product) / "local-settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) and isinstance(body.get("items"), dict) else None
