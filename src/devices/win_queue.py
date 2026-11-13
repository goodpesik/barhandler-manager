"""State of a Windows print-spooler queue (win32print).

The spooler accepts a RAW job even when nothing can print it: the queue may be
offline, paused, in error, or blocked behind earlier jobs. Win32Raw only tells
us that the job was accepted. This module reads what the spooler itself knows
about the queue (GetPrinter level 2) and whether a sent job has left it, so a
test print is not reported as printed while it sits in the queue.

Every reader returns None when win32print is unavailable (any non-Windows
platform) or the spooler call fails: callers then keep their old behaviour.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# winspool.h PRINTER_STATUS_* / PRINTER_ATTRIBUTE_* values.
_STATUS_NAMES = {
    0x00000001: "paused",
    0x00000002: "error",
    0x00000004: "pending_deletion",
    0x00000008: "paper_jam",
    0x00000010: "paper_out",
    0x00000020: "manual_feed",
    0x00000040: "paper_problem",
    0x00000080: "offline",
    0x00000100: "io_active",
    0x00000200: "busy",
    0x00000400: "printing",
    0x00000800: "output_bin_full",
    0x00001000: "not_available",
    0x00002000: "waiting",
    0x00004000: "processing",
    0x00008000: "initializing",
    0x00010000: "warming_up",
    0x00020000: "toner_low",
    0x00040000: "no_toner",
    0x00080000: "page_punt",
    0x00100000: "user_intervention",
    0x00200000: "out_of_memory",
    0x00400000: "door_open",
    0x00800000: "server_unknown",
    0x01000000: "power_save",
}
_ATTRIBUTE_WORK_OFFLINE = 0x00000400

# Allow-list of the queue states that block printing, in the order they are
# reported, each with the error code the admin app receives. The paper and cover
# codes are the ones the app already shows for the ESC/POS real-time status.
# Toner and output-bin states are not listed: they do not apply to thermal
# receipt printers.
_BLOCKING = (
    ({"offline", "not_available", "work_offline"}, "queue_offline", "the queue is offline"),
    ({"paused"}, "queue_paused", "the queue is paused"),
    ({"paper_out"}, "out_of_paper", "the spooler reports paper out"),
    ({"door_open"}, "cover_open", "the spooler reports the cover open"),
    (
        {"error", "paper_jam", "paper_problem", "user_intervention",
         "pending_deletion", "server_unknown"},
        "queue_error",
        "the queue is in error",
    ),
)


@dataclass(frozen=True)
class QueueState:
    name: str
    port: Optional[str]
    driver: Optional[str]
    status: int
    attributes: int
    jobs: int

    @property
    def flags(self) -> list[str]:
        names = [label for bit, label in _STATUS_NAMES.items() if self.status & bit]
        if self.attributes & _ATTRIBUTE_WORK_OFFLINE:
            names.append("work_offline")
        return names

    def problem(self) -> Optional[tuple[str, str]]:
        """(code, reason) when the queue cannot print, otherwise None."""
        present = set(self.flags)
        for flags, code, text in _BLOCKING:
            hit = sorted(present & flags)
            if hit:
                return code, f"{self.name}: {text} ({', '.join(hit)})"
        return None

    def as_dict(self) -> dict:
        return {
            "port": self.port,
            "driver": self.driver,
            "status": self.status,
            "status_flags": self.flags,
            "jobs": self.jobs,
        }

    def describe(self) -> str:
        """One log-friendly line: queue, port, driver, flags, job count."""
        return (
            f"queue={self.name!r} port={self.port} driver={self.driver!r} "
            f"status=0x{self.status:x} flags={','.join(self.flags) or '-'} jobs={self.jobs}"
        )


def _win32print():
    try:
        import win32print  # type: ignore
    except Exception:  # noqa: BLE001 — not Windows / pywin32 absent
        return None
    return win32print


def read_queue(name: str) -> Optional[QueueState]:
    """Read one queue's state. None off Windows or when the spooler refuses."""
    w = _win32print()
    if w is None or not name:
        return None
    try:
        handle = w.OpenPrinter(name)
        try:
            info = w.GetPrinter(handle, 2)
        finally:
            w.ClosePrinter(handle)
        return QueueState(
            name=name,
            port=info.get("pPortName"),
            driver=info.get("pDriverName"),
            status=int(info.get("Status") or 0),
            attributes=int(info.get("Attributes") or 0),
            jobs=int(info.get("cJobs") or 0),
        )
    except Exception as exc:  # noqa: BLE001 — pywintypes.error and friends
        logger.warning("spooler queue %r could not be read: %s", name, exc)
        return None


# winspool.h JOB_STATUS_* bits of a job that has already finished: with
# "Keep printed documents" on, EnumJobs still lists it.
_JOB_DONE = 0x00000080 | 0x00000100 | 0x00001000  # PRINTED | DELETED | COMPLETE


def _job_present(name: str, job_id: int) -> Optional[bool]:
    """Is `job_id` still waiting in the queue? A finished job counts as gone."""
    w = _win32print()
    if w is None or not name:
        return None
    try:
        handle = w.OpenPrinter(name)
        try:
            jobs = w.EnumJobs(handle, 0, 999, 1)
        finally:
            w.ClosePrinter(handle)
    except Exception as exc:  # noqa: BLE001
        logger.warning("spooler jobs of %r could not be read: %s", name, exc)
        return None
    return any(
        job.get("JobId") == job_id and not int(job.get("Status") or 0) & _JOB_DONE
        for job in jobs
    )


# How long a test print waits for its job to leave the queue. A healthy USB
# receipt printer takes the job within a second or two.
DRAIN_WAIT_S = 4.0
_DRAIN_POLL_S = 0.25


async def wait_until_left(name: str, job_id: Optional[int]) -> Optional[QueueState]:
    """Wait (bounded) for the sent job to leave the queue.

    Returns None when the job left the queue, or when the spooler cannot be
    asked or the job id is unknown (the old behaviour: report printed).
    Returns the queue's state when the job is still queued after
    DRAIN_WAIT_S.
    """
    if job_id is None:
        logger.info("spool job on %r: job id not available, reported as sent", name)
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DRAIN_WAIT_S
    while True:
        present = await asyncio.to_thread(_job_present, name, job_id)
        if present is None:
            logger.info("spool job %s on %r: queue not readable, reported as sent", job_id, name)
            return None
        if not present:
            logger.info("spool job %s left queue %r", job_id, name)
            return None
        if loop.time() >= deadline:
            break
        await asyncio.sleep(_DRAIN_POLL_S)
    state = await asyncio.to_thread(read_queue, name)
    if state is None:
        state = QueueState(name=name, port=None, driver=None, status=0, attributes=0, jobs=-1)
    logger.warning(
        "spool job %s still queued after %.0fs: %s", job_id, DRAIN_WAIT_S, state.describe(),
    )
    return state


def list_queues() -> Optional[list[QueueState]]:
    """Every local and connected queue with its state. None off Windows."""
    w = _win32print()
    if w is None:
        return None
    flags = w.PRINTER_ENUM_LOCAL | w.PRINTER_ENUM_CONNECTIONS
    states = []
    for entry in w.EnumPrinters(flags, None, 1):
        name = entry[2]
        if not name:
            continue
        state = read_queue(name)
        states.append(state or QueueState(name, None, None, 0, 0, -1))
    return states
