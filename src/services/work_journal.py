"""PET-1087 — the work journal of this station's printers and terminals.

What a person wants to know about a device standing in their shop is not in
any of our logs: did it print, when, and did it go through. Until now the
only trace of a print was a line in `bhm.log` on that very machine, rotated
away in a day, and nothing at all reached the shop's own records.

So every print job and every card operation leaves a row here. The file is
local (the station owns its journal, like `printers.json`), the rows are
numbered by a sequence that never goes backwards, and the online app carries
them to the server once an hour by that number — hence `after_seq`: a resend
brings the same rows and the server keeps one of each.

TWO THINGS THE NUMBERING MUST NEVER DO (both found by review):

* START AGAIN FROM 1. The server keys a row by (station, number) and ignores
  a number it already has. If this file is deleted, wiped by an update or
  found unreadable, a sequence that began again at 1 would make the station's
  next few hundred rows look like duplicates of rows the server took months
  ago — and they would be dropped in silence. So when nothing readable is
  found, the sequence starts from a floor taken from the clock (seconds since
  the epoch), which is far above any count of print jobs and never goes back.

* HAND OUT THE SAME NUMBER TWICE. A row whose write failed is NOT kept in
  memory: it is not served to the app, and its number is burned rather than
  reused. A gap in the numbers costs nothing (the server only needs them to
  grow); a repeated number costs a row.

The journal is also BOOKKEEPING: it is written after the deed, and a write
that fails must never break the deed it was describing. Every caller goes
through `record()`, which swallows its own failures into a warning.

One row is one line (`work-journal.jsonl`): a print appends a line instead of
rewriting the whole file, because `record()` is called from the event loop
that also drives the card terminal. The file is rewritten only when it has
grown past twice the limit.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("work-journal.jsonl")

#: How many rows the file keeps. A till prints a few hundred receipts a day,
#: and the rows go to the server every hour, so this is days of slack.
DEFAULT_LIMIT = 3000

#: Which device the row is about.
KIND_PRINTER = "printer"
KIND_TERMINAL = "terminal"

#: How the deed ended.
#:
#: `cancelled` and `declined` are their own answers, not failures: nobody is
#: at fault when an operator stops a payment, and a card that says no is not a
#: broken terminal. Calling either a failure sends a shop looking for a fault
#: that is not there — which is the whole reason this journal exists.
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_DECLINED = "declined"

#: What a terminal was asked to do. The receipt side has no fixed list: the
#: action of a print is the route that printed it (`receipt`, `label`, …).
ACTION_SALE = "sale"
ACTION_REFUND = "refund"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _seq_floor() -> int:
    """A number no earlier life of this journal can have reached.

    Seconds since the epoch: about 1.8e9 today, growing by one a second. A
    station printing a receipt every second for a year would add 3.1e7 — so
    the floor of the next life always stands above the numbers of this one,
    and it stays far below the server's own ceiling on a row number.
    """
    return int(time.time())


class WorkJournal:
    def __init__(self, path: Path = DEFAULT_PATH, limit: int = DEFAULT_LIMIT) -> None:
        self.path = Path(path)
        self.limit = max(1, int(limit))
        self._entries: list[dict] = []
        self._seq = 0
        #: Lines on disk, so compaction is decided without reading the file.
        self._lines = 0
        # Rows are written from the event loop and from device worker threads
        # (a print finishes off-loop), so the file is guarded by a lock.
        self._lock = threading.Lock()

    # ---------- persistence ----------

    def load(self) -> None:
        rows: list[dict] = []
        unreadable = 0
        existed = self.path.exists()
        if existed:
            try:
                with self.path.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except Exception:  # noqa: BLE001 — one bad line, not a crash
                            unreadable += 1
                            continue
                        if isinstance(row, dict) and isinstance(row.get("seq"), int):
                            rows.append(row)
                        else:
                            unreadable += 1
            except OSError as exc:
                logger.warning("work-journal could not be read: %s", exc)
        rows.sort(key=lambda e: e["seq"])
        highest = rows[-1]["seq"] if rows else 0
        with self._lock:
            self._entries = rows[-self.limit :]
            self._lines = len(rows)
            # The floor applies when there is nothing to continue from: a fresh
            # install, a deleted file, a file we could not read. Continuing from
            # 0 there is what would hand the server numbers it already has.
            self._seq = highest if highest else _seq_floor()
        if unreadable:
            logger.warning("work-journal: %s line(s) could not be read and are skipped", unreadable)
        logger.info(
            "work-journal: %s row(s), the numbering continues from %s%s",
            len(rows),
            self._seq,
            "" if highest else " (a floor taken from the clock: nothing to continue from)",
        )

    def _append_locked(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)
        self._lines += 1

    def _compact_locked(self) -> None:
        """Rewrite the file with the rows we keep. Only when it has grown past
        twice the limit, so the cost falls on one row in thousands."""
        if self._lines <= self.limit * 2:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        # Written beside the journal and moved over it: a crash mid-write
        # leaves the previous file whole.
        with tmp.open("w", encoding="utf-8") as f:
            for row in self._entries:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)
        self._lines = len(self._entries)
        logger.info("work-journal: the file is rewritten, %s row(s) kept", self._lines)

    # ---------- writing ----------

    def record(
        self,
        *,
        device_kind: str,
        device_id: str,
        action: str,
        status: str,
        amount_kopecks: Optional[int] = None,
        detail: Optional[str] = None,
        at: Optional[str] = None,
    ) -> Optional[dict]:
        """Append one row. Answers the row, or None when nothing was written.

        Never raises: the deed has already happened, and a journal that cannot
        be written is a line in the log, not a failed sale.
        """
        if not device_id or not action or not status:
            logger.warning(
                "work-journal: a row without device/action/status is not written (%s %s %s)",
                device_id, action, status,
            )
            return None
        try:
            with self._lock:
                self._seq += 1
                entry = {
                    "seq": self._seq,
                    "at": at or _now_iso(),
                    "deviceKind": device_kind,
                    "deviceId": device_id,
                    "action": action,
                    "status": status,
                    "amount": amount_kopecks,
                    "detail": (detail or None),
                }
                try:
                    self._append_locked(entry)
                except Exception as exc:  # noqa: BLE001 — see the docstring
                    # The number is burned, not reused: a row the app never got
                    # must not come back later under a number the server has.
                    logger.warning(
                        "work-journal: row %s was not written and is dropped: %s", entry["seq"], exc,
                    )
                    return None
                self._entries.append(entry)
                if len(self._entries) > self.limit:
                    del self._entries[: len(self._entries) - self.limit]
                try:
                    self._compact_locked()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("work-journal: the file was not rewritten: %s", exc)
                return dict(entry)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            logger.warning("work-journal: the row was not written: %s", exc)
            return None

    # ---------- reading ----------

    def page(self, after_seq: int = 0, limit: int = 500) -> dict:
        """The rows after `after_seq`, and the two numbers that go with them.

        `pageLastSeq` is THE CURSOR to ask from next time — the last row in
        this page. `lastSeq` is the highest number on the station, so the
        caller can tell how much is still queued. Taking the station's highest
        as a cursor would skip everything between the page and it, which is
        exactly the mistake this answer is shaped to prevent (found by review),
        and both are read under one lock so a row written in between cannot
        land between the two numbers either.
        """
        after = max(0, int(after_seq))
        take = max(1, min(int(limit), 2000))
        with self._lock:
            rows = [dict(e) for e in self._entries if e["seq"] > after][:take]
            return {
                "entries": rows,
                "pageLastSeq": rows[-1]["seq"] if rows else after,
                "lastSeq": self._seq,
            }

    def entries(self, after_seq: int = 0, limit: int = 500) -> list[dict]:
        return self.page(after_seq, limit)["entries"]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._entries)


# Module-level singleton — the device layer records through it without having
# to be handed the journal (same shape as `log_uplink._active`). A manager
# built without one (tests of the print path, an old embedded build) records
# nothing and prints exactly as before.
_active: Optional[WorkJournal] = None


def set_active(journal: Optional[WorkJournal]) -> None:
    global _active
    _active = journal


def get_active() -> Optional[WorkJournal]:
    return _active


def record_printer(
    device_id: str,
    action: str,
    status: str,
    detail: Optional[str] = None,
) -> None:
    """A print job ended. Called from the printer's own worker, so it covers
    every route that prints — including the ones written after this one."""
    journal = _active
    if journal is None:
        return
    journal.record(
        device_kind=KIND_PRINTER,
        device_id=device_id,
        action=action,
        status=status,
        detail=detail,
    )


def record_terminal(
    device_id: str,
    action: str,
    status: str,
    amount_kopecks: Optional[int] = None,
    detail: Optional[str] = None,
) -> None:
    """A card operation ended — the sum belongs to the row, it is what the
    shop recognises the operation by."""
    journal = _active
    if journal is None:
        return
    journal.record(
        device_kind=KIND_TERMINAL,
        device_id=device_id,
        action=action,
        status=status,
        amount_kopecks=amount_kopecks,
        detail=detail,
    )
