"""BH-182 — a manager running for days stopped printing on a Mac.

The frozen build runs from the system temp folder; macOS (and Windows) clean
it of files untouched for days. After four days of uptime the fonts were gone
and every print failed with Pillow's «cannot open resource» — reported as a
lost connection, the receipt printer shown offline.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import pytest

from src.devices import printer as printer_module
from src.devices.printer import PrinterDevice, PrintRenderError, PrinterUnavailable
from src.services import bitmap_render, extract_keeper


# ---------- the extraction folder is kept fresh ----------

def _age(path, days: float) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_touch_tree_refreshes_every_file_and_folder(tmp_path) -> None:
    fonts = tmp_path / "src" / "assets" / "fonts"
    fonts.mkdir(parents=True)
    ttf = fonts / "NotoSansMono-Regular.ttf"
    ttf.write_bytes(b"x")
    for p in (ttf, fonts, tmp_path / "src"):
        _age(p, 10)

    touched, failed = extract_keeper.touch_tree(str(tmp_path))

    assert failed == 0
    assert touched == 4  # src, assets, fonts, the file
    for p in (ttf, fonts, tmp_path / "src"):
        assert time.time() - os.stat(p).st_atime < 60
        assert time.time() - os.stat(p).st_mtime < 60


def test_not_frozen_means_nothing_to_keep(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert extract_keeper.extraction_dir() is None


def test_frozen_keeps_meipass(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert extract_keeper.extraction_dir() == str(tmp_path)


@pytest.mark.asyncio
async def test_the_keeper_touches_on_start_and_keeps_going(tmp_path) -> None:
    f = tmp_path / "cacert.pem"
    f.write_bytes(b"x")
    _age(f, 10)
    task = asyncio.create_task(
        extract_keeper.keep_extraction_alive(str(tmp_path), every=0.05, check=0.01),
    )
    try:
        await asyncio.sleep(0.2)
        assert time.time() - os.stat(f).st_mtime < 60, "not touched on start"
        _age(f, 10)
        await asyncio.sleep(0.2)
        assert time.time() - os.stat(f).st_mtime < 60, "not touched again"
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_the_keeper_goes_by_the_wall_clock_so_sleep_counts(monkeypatch, tmp_path) -> None:
    """Review: asyncio.sleep stops while a Mac sleeps. A closed laptop must
    still get its files touched right after it wakes."""
    f = tmp_path / "cacert.pem"
    f.write_bytes(b"x")
    wall = [time.time()]
    monkeypatch.setattr(extract_keeper.time, "time", lambda: wall[0])
    task = asyncio.create_task(
        extract_keeper.keep_extraction_alive(str(tmp_path), every=3600, check=0.01),
    )
    try:
        await asyncio.sleep(0.1)
        _age(f, 10)
        await asyncio.sleep(0.1)
        # Barely any running time passed, so nothing is due yet.
        assert time.time() - os.stat(f).st_mtime > 86400
        wall[0] += 3 * 86400  # the laptop slept three days
        await asyncio.sleep(0.1)
        assert os.stat(f).st_mtime >= wall[0] - 60, "not touched after the long sleep"
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_a_failed_pass_does_not_stop_the_keeper(monkeypatch, tmp_path) -> None:
    calls = []

    def _flaky(root):
        calls.append(root)
        if len(calls) == 1:
            raise RuntimeError("odd file name")
        return 1, 0

    monkeypatch.setattr(extract_keeper, "touch_tree", _flaky)
    task = asyncio.create_task(
        extract_keeper.keep_extraction_alive(str(tmp_path), every=0, check=0.01),
    )
    try:
        await asyncio.sleep(0.1)
        assert not task.done(), "the keeper died on one bad pass"
        assert len(calls) >= 2

    finally:
        task.cancel()

@pytest.mark.asyncio
async def test_a_lasting_failure_logs_the_traceback_once(monkeypatch, tmp_path, caplog) -> None:
    def _broken(root):
        raise RuntimeError("still broken")

    monkeypatch.setattr(extract_keeper, "touch_tree", _broken)
    caplog.set_level("WARNING", logger=extract_keeper.logger.name)
    task = asyncio.create_task(
        extract_keeper.keep_extraction_alive(str(tmp_path), every=0, check=0.01),
    )
    try:
        await asyncio.sleep(0.15)
    finally:
        task.cancel()
    with_trace = [r for r in caplog.records if r.exc_info]
    assert len(with_trace) == 1
    assert any("failed again" in r.getMessage() for r in caplog.records)


def test_the_server_starts_the_keeper() -> None:
    src = open("src/server.py", encoding="utf-8").read()
    assert "keep_extraction_alive(extract_root)" in src
    assert "extract_keeper.cancel()" in src


# ---------- rendering does not need the font files after start ----------

def test_rendering_works_when_the_font_files_are_gone(monkeypatch, tmp_path) -> None:
    # The fonts were read at import; now the files disappear, as they did
    # after four days in the temp folder.
    monkeypatch.setattr(bitmap_render, "FONT_REGULAR", tmp_path / "gone-regular.ttf")
    monkeypatch.setattr(bitmap_render, "FONT_BOLD", tmp_path / "gone-bold.ttf")

    img = bitmap_render.render_paragraph("ТЕСТ ДРУКУ\nїґє", width_px=384, bold=True)
    assert img.size[0] == 384
    assert bitmap_render.measure("abc")[0] > 0


def test_the_fonts_are_in_memory() -> None:
    assert bitmap_render._FONT_BYTES[False]
    assert bitmap_render._FONT_BYTES[True]


# ---------- a render failure is not a lost connection ----------

class _Escpos:
    def __init__(self) -> None:
        self.raw: list[bytes] = []
        self.closed = False

    def _raw(self, data) -> None:
        self.raw.append(bytes(data))

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_a_font_error_is_reported_as_render_failure_and_keeps_the_printer() -> None:
    dev = PrinterDevice(
        "t", {"enabled": True, "paper_width": 80, "render_mode": "native", "code_page": None},
    )
    fake = _Escpos()
    dev._printer = fake
    dev._worker_task = asyncio.create_task(dev._worker())

    async def _render_fails(_esc) -> None:
        raise OSError("cannot open resource")

    async def _good(esc) -> None:
        esc._raw(b"NEXT")

    try:
        with pytest.raises(PrintRenderError) as err:
            await asyncio.wait_for(dev.enqueue(_render_fails), timeout=5)
        # Review: the /print routes map PrinterUnavailable to a structured 503;
        # a render failure must reach the frontend the same way, with its own code.
        assert isinstance(err.value, PrinterUnavailable)
        assert err.value.code == "render_failed"
        assert "cannot open resource" in str(err.value)
        # Not marked disconnected: the next print goes through the same handle.
        assert dev._printer is fake
        await asyncio.wait_for(dev.enqueue(_good), timeout=5)
    finally:
        dev._worker_task.cancel()

    assert fake.raw == [b"NEXT"]
    assert fake.closed is False
