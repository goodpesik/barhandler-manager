"""Reading the station's own registry files (printers.json, terminals.json).

Both files are written as UTF-8. Older managers wrote them in the
station's code page, and such files are still on the tills, so a read
tries UTF-8 first and the code page only for a file that is not valid
UTF-8. Cyrillic in a single-byte code
page is never valid UTF-8, so the two cannot be confused.
"""

from __future__ import annotations

import codecs
import locale
import logging
import os
from pathlib import Path

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
        return data.decode("utf-8"), True
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
