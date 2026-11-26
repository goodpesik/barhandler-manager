"""Reading the station's own registry files (printers.json, terminals.json).

Both files are written as UTF-8. Older managers wrote them in the
station's code page, and such files are still on the tills, so a read
tries UTF-8 first (with or without a byte order mark, which Notepad
adds) and the code page only for a file that is not valid UTF-8.
Cyrillic text in a single-byte code page is valid UTF-8 only by rare
accident (a short pair such as "Рё"); a whole file of nicknames and
JSON practically never is.
"""

from __future__ import annotations

import codecs
import json
import locale
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def station_encoding() -> str:
    """The code page an older manager wrote a registry file in.

    That manager used the station's own encoding, which on a Ukrainian
    Windows is cp1251 and on a Western one cp1252 — the ANSI code page,
    whatever `read_text()` without an encoding picks. It is only ever
    used to READ such a file; nothing is written in it any more.

    On Windows that is the ANSI code page itself (`mbcs`), not the
    locale setting of today's interpreter: the file was written by a
    build that had no say in the matter, and this one could be started
    in UTF-8 mode, which would leave the old file unreadable and the
    shop with no devices.
    """
    if os.name == "nt":
        return "mbcs"
    return locale.getpreferredencoding(False) or "utf-8"


def read_station_text(path: Path) -> tuple[str, bool]:
    """The contents of `path` and whether they were stored as UTF-8."""
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig"), True
    except UnicodeDecodeError:
        fallback = station_encoding()
        if codecs.lookup(fallback).name == "utf-8":
            # Nothing else to try — the file is simply broken, and the
            # re-raise has to happen in here while that is still the
            # exception being handled; outside, a bare `raise` is a
            # RuntimeError and the log would name the wrong reason.
            raise
    text = data.decode(fallback)
    logger.info(
        "%s was written by an older manager in this station's code page "
        "(%s) — read as such; it is stored as UTF-8 from the next save on",
        path, fallback,
    )
    return text, False


def keep_unreadable_copy(path: Path) -> Optional[str]:
    """Copy a registry file this version cannot read next to it.

    The registry starts empty after such a load, and the next save
    writes only what is in memory, so without this copy the shop's
    devices would be gone for good. Answers why saving must be refused
    for this run when the copy itself fails (the file then stays as it
    is), and None when the copy is kept.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    kept = path.with_name(f"{path.name}.unreadable-{stamp}")
    try:
        shutil.copyfile(path, kept)
    except OSError as exc:
        refused = f"{path.name} is unreadable and could not be copied aside: {exc}"
        logger.warning("%s; device changes are not stored in this run", refused)
        return refused
    logger.warning("unreadable %s copied to %s before any change", path.name, kept)
    return None


def write_station_json(path: Path, payload: dict) -> None:
    """Replace `path` with `payload` in one step, as UTF-8.

    Written to a temporary file in the same folder (so the rename stays
    on one filesystem and is atomic), forced to the disk, then renamed
    over the old file: a power cut at any point leaves either the old
    file or the new one, never a half-written or zero-length one.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2, ensure_ascii=False))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
