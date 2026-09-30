"""BH-182 — keep the PyInstaller onefile extraction folder alive.

The frozen build is one file: at start it unpacks its code and data into the
system temp folder (`_MEIPASS`, e.g. /var/folders/…/T/_MEIxxxx on a Mac) and
runs from there for as long as the process lives. Both macOS and Windows clean
their temp folders of files nobody touched for a few days. A manager that ran
four days without a restart lost its fonts that way — every print then failed
with Pillow's «cannot open resource» — and anything else read after start
(certifi's CA bundle for HTTPS, usb_probe.py, VERSION) is exposed the same way.

`runtime_tmpdir` cannot move the folder somewhere safe: on POSIX the bootloader
does not expand `~`/`$HOME`, and the home folder differs per machine. So the
manager keeps its own files fresh instead: every few hours it bumps the access
and modification times of everything under `_MEIPASS`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

logger = logging.getLogger(__name__)

# The cleanup takes files untouched for about three days; twice a day is far
# inside that and costs nothing.
KEEP_EVERY_S = 12 * 3600

# Found by review: `asyncio.sleep` runs on the monotonic clock, which on macOS
# stops while the Mac sleeps — a 12-hour sleep could span days of a closed
# laptop. So the keeper wakes every minute and decides by the wall clock,
# which does count sleep: after a long sleep it touches within a minute.
CHECK_EVERY_S = 60

def extraction_dir() -> str | None:
    """The onefile extraction folder, or None when not running frozen."""
    if not getattr(sys, "frozen", False):
        return None
    return getattr(sys, "_MEIPASS", None)


def touch_tree(root: str) -> tuple[int, int]:
    """Bump atime/mtime of every file and folder under `root`.

    Returns (touched, failed). A file that is already gone is counted as
    failed — that is exactly what the log line has to show.
    """
    now = time.time()
    touched = failed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            try:
                os.utime(os.path.join(dirpath, name), (now, now))
                touched += 1
            except OSError:
                failed += 1
    try:
        os.utime(root, (now, now))
    except OSError:
        failed += 1
    return touched, failed


async def keep_extraction_alive(
    root: str, every: float = KEEP_EVERY_S, check: float = CHECK_EVERY_S,
) -> None:
    """Touch the extraction folder now and then whenever `every` seconds of
    wall-clock time have passed, looking every `check` seconds."""
    logger.info("[extract-keeper] keeping %s fresh every %.0f h", root, every / 3600)
    last = None
    failures = 0
    while True:
        if last is None or time.time() - last >= every:
            # Found by review: one failed pass must not end the task — it would
            # die silently and the files would be cleaned days later anyway.
            try:
                touched, failed = await asyncio.to_thread(touch_tree, root)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                # The traceback once; after that one short line a minute, so a
                # lasting failure does not flood the log uplink.
                if failures == 1:
                    logger.exception("[extract-keeper] pass over %s failed", root)
                else:
                    logger.warning(
                        "[extract-keeper] pass over %s failed again (%d): %s", root, failures, exc,
                    )
            else:
                failures = 0
                last = time.time()
                if failed:
                    logger.warning(
                        "[extract-keeper] touched %d, failed %d under %s", touched, failed, root,
                    )
                else:
                    logger.info("[extract-keeper] touched %d entries under %s", touched, root)
        await asyncio.sleep(check)
