"""PET-972 — the offline service's secrets: the device token (lets the till
sync with the server) and the data key (encrypts the local copy of the
shop's data).

They live in one file in the shop's offline folder (0600 on macOS and Linux;
on Windows the per-user install folder's access rules apply), on every
system, and never go into config files or logs.

The operating system's stores (the macOS Keychain, DPAPI) were used at first
and dropped: the Keychain locks after sleep or a screen lock, a manager in
the background cannot unlock it, and a locked Keychain read as «no secrets»
stopped the till offline and refused a new activation. The token is issued
to a signed-in staff member and can be revoked from Petshandler.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _encode(secrets: dict) -> str:
    # Base64 of JSON: one ASCII line.
    return base64.b64encode(json.dumps(secrets).encode("utf-8")).decode("ascii")


def _decode(raw: str) -> Optional[dict]:
    try:
        value = json.loads(base64.b64decode(raw.strip()).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_private(path: Path, data: bytes) -> None:
    """Write through a temporary file and a rename, readable by this user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # A name of its own: two writers at once must not share (and steal) one
    # temporary file.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError:
        # A half-written temporary file may hold a secret: not left behind.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        log.warning("offline: %s could not be made private: %s", path.name, e)


#: The secrets file in the shop's folder.
SECRETS_FILE = "secrets.b64"


def save(appid: str, secrets: dict, folder: Path) -> None:
    """Keep the shop's secrets; `folder` is the shop's own offline folder."""
    _write_private(folder / SECRETS_FILE, _encode(secrets).encode("ascii"))
    log.info("offline secrets saved for shop %s", appid)


class SecretsUnreadable(OSError):
    """The secrets file is there but cannot be read now (permissions, a lock).

    Not the same as «no secrets»: the data key may well be in it, and a new
    one would make the local copy unreadable for good.
    """


def load(appid: str, folder: Path) -> Optional[dict]:
    """The shop's secrets; None when there are none, or the file is damaged.

    Raises SecretsUnreadable when the file is there but cannot be read.
    """
    try:
        raw = (folder / SECRETS_FILE).read_text(encoding="ascii")
    except FileNotFoundError:
        return None  # not activated, or switched off: the caller says so
    except (OSError, UnicodeDecodeError) as e:
        if isinstance(e, UnicodeDecodeError):
            log.warning("offline secrets for shop %s: the file is damaged", appid)
            return None
        log.warning("offline secrets for shop %s unreadable: %s", appid, e)
        raise SecretsUnreadable(str(e)) from e
    secrets = _decode(raw)
    if secrets is None:
        log.warning("offline secrets for shop %s: the file is damaged", appid)
    return secrets


def delete(appid: str, folder: Path) -> None:
    try:
        (folder / SECRETS_FILE).unlink()
    except FileNotFoundError:
        pass
    log.info("offline secrets removed for shop %s", appid)
