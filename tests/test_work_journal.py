"""PET-1087 — the work journal of a station's devices.

What these tests hold onto: the numbering the server reads rows by, the rule
that a journal never breaks the deed it describes, and that a print and a card
operation leave a row at all — with the sum, for the terminal.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from src.devices.printer import PrinterDevice, PrinterUnavailable
from src.services import work_journal as journal_service
from src.services.work_journal import WorkJournal


@pytest.fixture
def journal(tmp_path: Path) -> WorkJournal:
    return WorkJournal(path=tmp_path / "work-journal.json")


# ---------- numbering ----------


def test_rows_are_numbered_from_one_and_read_back_after_the_number(journal: WorkJournal) -> None:
    journal.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    journal.record(device_kind="printer", device_id="p1", action="label", status="ok")
    journal.record(device_kind="terminal", device_id="t1", action="sale", status="ok", amount_kopecks=12345)

    assert [e["seq"] for e in journal.entries()] == [1, 2, 3]
    after_first = journal.entries(after_seq=1)
    assert [e["action"] for e in after_first] == ["label", "sale"]
    assert after_first[-1]["amount"] == 12345
    assert journal.last_seq == 3


def test_the_numbering_continues_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "work-journal.json"
    first = WorkJournal(path=path)
    first.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    first.record(device_kind="printer", device_id="p1", action="receipt", status="ok")

    second = WorkJournal(path=path)
    second.load()
    row = second.record(device_kind="printer", device_id="p1", action="receipt", status="ok")

    assert row is not None and row["seq"] == 3
    assert [e["seq"] for e in second.entries()] == [1, 2, 3]


def test_a_dropped_row_does_not_hand_its_number_to_a_new_one(tmp_path: Path) -> None:
    """The rows the server has already taken are numbered; if a trimmed file
    restarted the count, the next row would arrive under a number the server
    knows and be dropped as a duplicate."""
    path = tmp_path / "work-journal.json"
    first = WorkJournal(path=path, limit=2)
    for _ in range(5):
        first.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    assert [e["seq"] for e in first.entries()] == [4, 5]

    second = WorkJournal(path=path, limit=2)
    second.load()
    assert second.last_seq == 5
    row = second.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    assert row is not None and row["seq"] == 6


def test_the_file_keeps_only_its_limit(tmp_path: Path) -> None:
    path = tmp_path / "work-journal.json"
    j = WorkJournal(path=path, limit=3)
    for _ in range(10):
        j.record(device_kind="printer", device_id="p1", action="receipt", status="ok")

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert len(stored["entries"]) == 3
    assert stored["seq"] == 10


def test_an_unreadable_journal_starts_a_new_one_instead_of_failing(tmp_path: Path) -> None:
    path = tmp_path / "work-journal.json"
    path.write_text("{not json", encoding="utf-8")
    j = WorkJournal(path=path)
    j.load()
    assert j.entries() == []
    assert j.record(device_kind="printer", device_id="p1", action="receipt", status="ok") is not None


# ---------- a journal never breaks the deed ----------


def test_a_journal_that_cannot_be_written_does_not_raise(tmp_path: Path) -> None:
    # A directory where the file should be: every write fails, as it would on
    # a read-only disk.
    path = tmp_path / "work-journal.json"
    path.mkdir()
    j = WorkJournal(path=path)
    assert j.record(device_kind="printer", device_id="p1", action="receipt", status="ok") is None


def test_a_row_without_a_device_or_an_action_is_not_written(journal: WorkJournal) -> None:
    assert journal.record(device_kind="printer", device_id="", action="receipt", status="ok") is None
    assert journal.record(device_kind="printer", device_id="p1", action="", status="ok") is None
    assert journal.entries() == []


# ---------- the print path ----------


class _Escpos:
    """A printer that prints, or refuses to — the `_raw` of python-escpos is
    what every mode goes through."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.raw: list[bytes] = []

    def _raw(self, data) -> None:
        if self.fail:
            raise OSError("the cable is out")
        self.raw.append(bytes(data))

    def cut(self, *_a, **_k) -> None:
        pass

    def set(self, **_k) -> None:
        pass

    def text(self, s) -> None:
        self._raw(str(s).encode("utf-8"))

    def close(self) -> None:
        pass


def _device() -> PrinterDevice:
    return PrinterDevice("printer-1", {"enabled": True, "paper_width": 80, "render_mode": "native", "code_page": None})


@pytest.fixture
def active_journal(journal: WorkJournal):
    journal_service.set_active(journal)
    yield journal
    journal_service.set_active(None)


@pytest.mark.asyncio
async def test_a_print_leaves_a_row_with_the_name_of_what_was_printed(active_journal: WorkJournal) -> None:
    dev = _device()
    dev._printer = _Escpos()
    dev._worker_task = asyncio.create_task(dev._worker())

    async def _job(esc) -> None:
        esc.text("RECEIPT")

    try:
        await asyncio.wait_for(dev.enqueue(_job, action="receipt"), timeout=5)
    finally:
        dev._worker_task.cancel()

    rows = active_journal.entries()
    assert [(r["deviceKind"], r["deviceId"], r["action"], r["status"]) for r in rows] == [
        ("printer", "printer-1", "receipt", "ok"),
    ]


@pytest.mark.asyncio
async def test_a_print_that_failed_says_so_and_says_why(active_journal: WorkJournal) -> None:
    dev = _device()
    dev._printer = _Escpos(fail=True)
    dev._worker_task = asyncio.create_task(dev._worker())

    async def _job(esc) -> None:
        esc.text("RECEIPT")

    try:
        with pytest.raises(Exception):
            await asyncio.wait_for(dev.enqueue(_job, action="label"), timeout=5)
    finally:
        dev._worker_task.cancel()

    rows = active_journal.entries()
    assert len(rows) == 1
    assert rows[0]["action"] == "label"
    assert rows[0]["status"] == "failed"
    assert rows[0]["detail"]


@pytest.mark.asyncio
async def test_a_print_into_a_disconnected_printer_is_a_row_too(active_journal: WorkJournal) -> None:
    """The commonest trouble of a shop — the printer is off. It never reaches
    the queue, so a journal written inside the worker would miss exactly the
    case somebody comes looking for."""
    dev = _device()
    with pytest.raises(PrinterUnavailable):
        await dev.enqueue(lambda _esc: asyncio.sleep(0), action="receipt")

    rows = active_journal.entries()
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert "not connected" in (rows[0]["detail"] or "")


@pytest.mark.asyncio
async def test_without_a_journal_a_print_still_prints() -> None:
    """A manager built without one (or already shut down) records nothing and
    changes nothing about printing."""
    journal_service.set_active(None)
    dev = _device()
    fake = _Escpos()
    dev._printer = fake
    dev._worker_task = asyncio.create_task(dev._worker())

    async def _job(esc) -> None:
        esc.text("RECEIPT")

    try:
        await asyncio.wait_for(dev.enqueue(_job, action="receipt"), timeout=5)
    finally:
        dev._worker_task.cancel()

    assert fake.raw == [b"RECEIPT"]


# ---------- rows are written from more than one thread ----------


def test_rows_written_at_once_keep_distinct_numbers(journal: WorkJournal) -> None:
    """A print finishes off the event loop, a card operation on it: two writers
    at once must not give two rows the same number, or the server would keep
    one of them."""

    def _write() -> None:
        for _ in range(25):
            journal.record(device_kind="printer", device_id="p1", action="receipt", status="ok")

    threads = [threading.Thread(target=_write) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    numbers = [e["seq"] for e in journal.entries(limit=2000)]
    assert len(numbers) == len(set(numbers)) == 100
