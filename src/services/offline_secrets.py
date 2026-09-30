"""PET-972 — the offline service's secrets, kept by the operating system.

Two secrets per activated shop: the device token (lets the till sync with the
server) and the data key (encrypts the local copy of the shop's data). They
never go into config files or logs:

* macOS — the login Keychain, through `security -i`: the command, secret
  included, is written to its stdin, so it never shows in the process list;
* Windows — DPAPI (CryptProtectData, bound to the Windows user), the sealed
  blob in a file only this user can read;
* elsewhere (a developer's Linux, the Android build) — a file with 0600
  permissions, the best that is available without a keystore.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

#: The Keychain service name; the account is the shop's appid.
KEYCHAIN_SERVICE = "device-handler-offline"


def _backend() -> str:
    """Where secrets go on this machine: keychain, dpapi or file."""
    if sys.platform == "darwin":
        return "keychain"
    if sys.platform == "win32":
        return "dpapi"
    return "file"


def _encode(secrets: dict) -> str:
    # Base64 of JSON: no spaces or quotes, safe inside a `security -i` line.
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
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---- macOS --------------------------------------------------------------


def _mac_run(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/usr/bin/security", "-i"],
        input=command + "\n",
        text=True,
        capture_output=True,
        timeout=15,
    )


def _mac_save(appid: str, secrets: dict) -> None:
    res = _mac_run(
        f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {appid} -w {_encode(secrets)}",
    )
    if res.returncode != 0:
        raise OSError(f"keychain refused to save (code {res.returncode})")


def _mac_load(appid: str) -> Optional[dict]:
    res = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", appid, "-w"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if res.returncode != 0:
        return None
    return _decode(res.stdout)


def _mac_delete(appid: str) -> None:
    subprocess.run(
        ["/usr/bin/security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", appid],
        capture_output=True,
        timeout=15,
    )


# ---- Windows ------------------------------------------------------------


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = fn(ctypes.byref(blob_in), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI refused")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # type: ignore[attr-defined]


# ---- the three calls ----------------------------------------------------


def save(appid: str, secrets: dict, folder: Path) -> None:
    """Keep the shop's secrets; `folder` is the shop's own offline folder."""
    backend = _backend()
    if backend == "keychain":
        _mac_save(appid, secrets)
    elif backend == "dpapi":
        _write_private(folder / "secrets.dpapi", _dpapi(_encode(secrets).encode("ascii"), protect=True))
    else:
        _write_private(folder / "secrets.b64", _encode(secrets).encode("ascii"))
    log.info("offline secrets saved for shop %s", appid)


def load(appid: str, folder: Path) -> Optional[dict]:
    try:
        backend = _backend()
        if backend == "keychain":
            return _mac_load(appid)
        if backend == "dpapi":
            return _decode(_dpapi((folder / "secrets.dpapi").read_bytes(), protect=False).decode("ascii"))
        return _decode((folder / "secrets.b64").read_text(encoding="ascii"))
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("offline secrets for shop %s unreadable: %s", appid, e)
        return None


def delete(appid: str, folder: Path) -> None:
    if _backend() == "keychain":
        _mac_delete(appid)
    for name in ("secrets.dpapi", "secrets.b64"):
        try:
            (folder / name).unlink()
        except FileNotFoundError:
            pass
    log.info("offline secrets removed for shop %s", appid)
