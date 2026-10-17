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
    return WorkJournal(path=tmp_path / "work-journal.jsonl")


# ---------- numbering ----------


def test_a_page_says_which_number_to_ask_from_next(journal: WorkJournal) -> None:
    for _ in range(5):
        journal.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    page = journal.page(after_seq=0, limit=2)
    assert [e["seq"] for e in page["entries"]] == [1, 2]
    assert page["pageLastSeq"] == 2, "the cursor is the page's own last row"
    assert page["lastSeq"] == 5, "the station's highest says how much is queued"

    rest = journal.page(after_seq=page["pageLastSeq"], limit=10)
    assert [e["seq"] for e in rest["entries"]] == [3, 4, 5]


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
    path = tmp_path / "work-journal.jsonl"
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
    path = tmp_path / "work-journal.jsonl"
    first = WorkJournal(path=path, limit=2)
    for _ in range(5):
        first.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    assert [e["seq"] for e in first.entries()] == [4, 5]

    second = WorkJournal(path=path, limit=2)
    second.load()
    assert second.last_seq == 5
    row = second.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    assert row is not None and row["seq"] == 6


def test_the_file_is_rewritten_once_it_outgrows_the_limit(tmp_path: Path) -> None:
    """One row is one line, so a print appends instead of rewriting half a
    megabyte on the loop that also drives the card terminal. The file is
    rewritten only when it has grown past twice the limit."""
    path = tmp_path / "work-journal.jsonl"
    j = WorkJournal(path=path, limit=3)
    for _ in range(6):
        j.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 6
    assert len(j.entries(limit=100)) == 3

    j.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3, "the file is rewritten with the rows it keeps"
    kept = [json.loads(line) for line in lines]
    assert [e["seq"] for e in kept] == [e["seq"] for e in j.entries(limit=100)]


def test_an_unreadable_journal_starts_a_new_one_instead_of_failing(tmp_path: Path) -> None:
    path = tmp_path / "work-journal.jsonl"
    path.write_text("{not json", encoding="utf-8")
    j = WorkJournal(path=path)
    j.load()
    assert j.entries() == []
    assert j.record(device_kind="printer", device_id="p1", action="receipt", status="ok") is not None


def test_a_lost_journal_does_not_start_numbering_again_from_one(tmp_path: Path) -> None:
    """THE finding of the review. The server keys a row by (station, number)
    and ignores a number it already has. If this file is deleted — an update,
    a wipe, a damaged disk — a sequence beginning again at 1 would make the
    station's next rows look like duplicates of rows the server took long ago,
    and they would vanish in silence. So a journal with nothing to continue
    from starts above any count of print jobs: the clock in seconds.
    """
    import time

    j = WorkJournal(path=tmp_path / "gone.jsonl")
    j.load()
    row = j.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    assert row is not None
    assert row["seq"] > 1_000_000_000, "a fresh journal must not hand out 1 again"
    assert row["seq"] <= int(time.time()) + 1


def test_a_corrupt_journal_also_starts_above_the_old_numbers(tmp_path: Path) -> None:
    path = tmp_path / "work-journal.jsonl"
    path.write_text("not json at all\n{]\n", encoding="utf-8")
    j = WorkJournal(path=path)
    j.load()
    row = j.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    assert row is not None and row["seq"] > 1_000_000_000


def test_a_journal_that_can_be_read_continues_its_own_numbers(tmp_path: Path) -> None:
    """The other half: a file that IS there must not jump to the clock, or the
    server's cursor would skip the rows still waiting in it."""
    path = tmp_path / "work-journal.jsonl"
    first = WorkJournal(path=path)
    first.record(device_kind="printer", device_id="p1", action="receipt", status="ok")

    second = WorkJournal(path=path)
    second.load()
    row = second.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    assert row is not None and row["seq"] == 2


def test_a_row_that_could_not_be_written_is_not_served_and_its_number_is_burned(
    tmp_path: Path,
) -> None:
    """Also from the review: a row kept in memory after a failed write is
    handed to the server under a number the file does not know. After a
    restart the same number is handed out again — and the server drops the
    second row as a duplicate. So the row goes, and the number is never
    reused.
    """
    path = tmp_path / "work-journal.jsonl"
    j = WorkJournal(path=path)
    j.load()
    first = j.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    assert first is not None

    def _boom(_entry: dict) -> None:
        raise OSError("the disk is full")

    j._append_locked = _boom  # noqa: SLF001 — the only way to fail one write
    assert j.record(device_kind="printer", device_id="p1", action="label", status="ok") is None
    assert [e["seq"] for e in j.entries(limit=100)] == [first["seq"]]

    del j._append_locked  # noqa: SLF001 — back to the real write
    after = j.record(device_kind="printer", device_id="p1", action="label", status="ok")
    assert after is not None
    assert after["seq"] == first["seq"] + 2, "the burned number is not handed out twice"


# ---------- a journal never breaks the deed ----------


def test_a_journal_that_cannot_be_written_does_not_raise(tmp_path: Path) -> None:
    # A directory where the file should be: every write fails, as it would on
    # a read-only disk.
    path = tmp_path / "work-journal.jsonl"
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


# ---------- the row tells the truth about the paper ----------


class _GatedEscpos:
    """A printer that holds the bytes until the gate opens — that is what any
    slow or wedged printer looks like from here."""

    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate
        self.raw: list[bytes] = []
        self.entered = threading.Event()

    def _raw(self, data) -> None:
        self.raw.append(bytes(data))
        self.entered.set()
        self.gate.wait(10.0)

    def cut(self, *_a, **_k) -> None:
        pass

    def set(self, **_k) -> None:
        pass

    def text(self, s) -> None:
        self._raw(str(s).encode("utf-8"))

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_a_caller_who_stopped_waiting_does_not_make_the_receipt_cancelled(
    active_journal: WorkJournal,
) -> None:
    """THE print finding of the review.

    The write to the printer runs in the device's own thread and is shielded:
    it cannot be cancelled and finishes on its own. A client that disconnects
    (or a request that times out) cancels only the WAITING, so the paper does
    come out — and the first version, which recorded around that wait, wrote
    «cancelled» about a receipt the customer is holding.
    """
    gate = threading.Event()
    fake = _GatedEscpos(gate)
    dev = _device()
    dev._printer = fake
    dev._worker_task = asyncio.create_task(dev._worker())

    async def _job(esc) -> None:
        esc.text("RECEIPT")

    waiting = asyncio.create_task(dev.enqueue(_job, action="receipt"))
    while not fake.entered.is_set():
        await asyncio.sleep(0.01)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    gate.set()
    # Let the write and its journal row finish.
    for _ in range(200):
        if active_journal.entries():
            break
        await asyncio.sleep(0.01)
    dev._worker_task.cancel()

    rows = active_journal.entries()
    assert len(rows) == 1
    assert rows[0]["action"] == "receipt"
    assert rows[0]["status"] == "ok", "the paper came out, so the row says so"


@pytest.mark.asyncio
async def test_a_job_that_will_never_print_is_a_cancelled_row(
    active_journal: WorkJournal,
) -> None:
    """The printer is being reconfigured: the queued jobs are woken with a
    refusal and will never reach the paper. Each one is a row, or the journal
    would go quiet exactly when somebody is looking for the missing receipt."""
    dev = _device()
    dev._printer = _Escpos()
    # No worker: the job stays in the queue until it is failed.
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    async def _job(_esc) -> None:
        pass

    await dev._queue.put((_job, done, "label"))
    dev._fail_queued_jobs()

    rows = active_journal.entries()
    assert [(r["action"], r["status"]) for r in rows] == [("label", "cancelled")]
    assert done.done() and isinstance(done.exception(), PrinterUnavailable)


@pytest.mark.asyncio
async def test_a_printer_taken_away_mid_write_still_reports_the_paper(
    active_journal: WorkJournal,
) -> None:
    """The other half of the same rule. The device is dropped from the cache
    (any re-registration does that) and its worker is cancelled WHILE the
    write is in the device thread. That write cannot be cancelled — it
    finishes — so the row waits for it instead of guessing.
    """
    gate = threading.Event()
    fake = _GatedEscpos(gate)
    dev = _device()
    dev._printer = fake
    dev._worker_task = asyncio.create_task(dev._worker())

    async def _job(esc) -> None:
        esc.text("RECEIPT")

    waiting = asyncio.create_task(dev.enqueue(_job, action="fiscal"))
    while not fake.entered.is_set():
        await asyncio.sleep(0.01)
    dev._worker_task.cancel()
    await asyncio.sleep(0.01)
    gate.set()
    for _ in range(200):
        if active_journal.entries():
            break
        await asyncio.sleep(0.01)
    waiting.cancel()

    rows = active_journal.entries()
    assert [(r["action"], r["status"]) for r in rows] == [("fiscal", "ok")]
