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

The journal is BOOKKEEPING: it is written after the deed, and a write that
fails must never break the deed it was describing. Every caller goes through
`record()`, which swallows its own failures into a warning.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("work-journal.json")

#: How many rows the file keeps. A till prints a few hundred receipts a day,
#: and the rows go to the server every hour, so this is days of slack — while
#: the file stays small enough to rewrite whole on every row.
DEFAULT_LIMIT = 3000

#: Which device the row is about.
KIND_PRINTER = "printer"
KIND_TERMINAL = "terminal"

#: How the deed ended. `cancelled` is its own answer: nobody is at fault and
#: nothing was printed, and calling that a failure would send a shop looking
#: for a broken printer.
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

#: What a terminal was asked to do. The receipt side has no fixed list: the
#: action of a print is the route that printed it (`receipt`, `label`, …).
ACTION_SALE = "sale"
ACTION_REFUND = "refund"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkJournal:
    def __init__(self, path: Path = DEFAULT_PATH, limit: int = DEFAULT_LIMIT) -> None:
        self.path = Path(path)
        self.limit = max(1, int(limit))
        self._entries: list[dict] = []
        self._seq = 0
        # Rows are written from the event loop and from device worker threads
        # (a print finishes off-loop), so the file is guarded by a lock.
        self._lock = threading.Lock()

    # ---------- persistence ----------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — a journal is never worth a crash
            logger.warning("work-journal unreadable, starting a new one: %s", exc)
            return
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            logger.warning("work-journal has no rows, starting a new one")
            return
        kept = [e for e in entries if isinstance(e, dict) and isinstance(e.get("seq"), int)]
        kept.sort(key=lambda e: e["seq"])
        with self._lock:
            self._entries = kept[-self.limit :]
            # The sequence continues from the highest number ever written, not
            # from the number of rows: dropped rows must not hand their numbers
            # to new ones, or the server would take the new row for a duplicate
            # of a row it already has.
            stored_seq = raw.get("seq") if isinstance(raw, dict) else None
            highest = kept[-1]["seq"] if kept else 0
            self._seq = max(int(stored_seq) if isinstance(stored_seq, int) else 0, highest)
        logger.info("work-journal: %s rows, last number %s", len(kept), self._seq)

    def _save_locked(self) -> None:
        payload = {"seq": self._seq, "entries": self._entries}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        # Written beside the journal and moved over it: a crash mid-write
        # leaves the previous file whole instead of half a JSON document.
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

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
                self._entries.append(entry)
                if len(self._entries) > self.limit:
                    del self._entries[: len(self._entries) - self.limit]
                self._save_locked()
                return dict(entry)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            logger.warning("work-journal: the row was not written: %s", exc)
            return None

    # ---------- reading ----------

    def entries(self, after_seq: int = 0, limit: int = 500) -> list[dict]:
        after = max(0, int(after_seq))
        take = max(1, min(int(limit), 2000))
        with self._lock:
            return [dict(e) for e in self._entries if e["seq"] > after][:take]

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
    """A print job ended. Called from the printer's own queue, so it covers
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
