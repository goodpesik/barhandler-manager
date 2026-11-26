"""PrinterRegistry persistence — PET-1304.

Only what this ticket is about: the shop's printers survive a save
that does not finish, and a Cyrillic nickname comes back the way the
operator typed it. The rest of the registry's behaviour is covered by
`test_discover_and_register.py` and `test_tspl_receipt.py`.
"""

from __future__ import annotations

import builtins
import io
import json
import os
from pathlib import Path

import pytest

from src.devices import station_text
from src.devices.registry import PrinterRegistry
from src.models.printer import PrinterKind, RegistrationRequest


def _registry_with_one_printer(path: Path, nickname: str = "Bar") -> PrinterRegistry:
    registry = PrinterRegistry(path=path)
    descriptor = registry.add_manual_descriptor(host="10.0.0.12", port=9100)
    registry.register(RegistrationRequest(
        id=descriptor.id, kind=PrinterKind.receipt, nickname=nickname,
    ))
    return registry


class _DiesMidWrite:
    """A handle that takes the first few characters and then the power
    goes — the kill, the crash and the power cut this ticket is about,
    all of which land between the truncation and the last byte."""

    def __init__(self, handle: object) -> None:
        self._handle = handle

    def write(self, text: str) -> int:
        self._handle.write(text[:8])
        self._handle.flush()
        raise OSError("the power went out mid-write")

    def __enter__(self) -> "_DiesMidWrite":
        return self

    def __exit__(self, *exc: object) -> None:
        self._handle.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._handle, name)


def _break_writes_under(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    """Every file opened for writing in `folder` dies half-done.

    Both seams are patched, because both are a way to write this file:
    `io.open` is what `Path.write_text` goes through, `builtins.open`
    is the bare `open()` in `save()`. Patching only the one `save()`
    happens to use today would let a return to writing the file in
    place pass this test.
    """
    for module, real in ((io, io.open), (builtins, builtins.open)):
        def opener(file, mode="r", *args, _real=real, **kwargs):  # type: ignore[no-untyped-def]
            handle = _real(file, mode, *args, **kwargs)
            if "w" in mode and isinstance(file, (str, bytes, os.PathLike)):
                if Path(os.fsdecode(file)).parent == folder:
                    return _DiesMidWrite(handle)
            return handle

        monkeypatch.setattr(module, "open", opener)


def test_a_save_that_dies_mid_write_leaves_every_printer_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loss this ticket exists for. printers.json used to be
    truncated and then written, so a kill in between left a stub —
    which reads as no printers at all, and the first register() after
    that writes the one new printer over the remains of the shop's
    others."""
    path = tmp_path / "printers.json"
    registry = _registry_with_one_printer(path)
    stored_before = path.read_bytes()
    assert stored_before, "nothing was stored to begin with"

    _break_writes_under(monkeypatch, tmp_path)
    with pytest.raises(OSError):
        registry.save()
    monkeypatch.undo()

    assert path.read_bytes() == stored_before, "the stored file was touched"
    reloaded = PrinterRegistry(path=path)
    reloaded.load()
    assert len(reloaded.all_registrations()) == 1


def _default_text_encoding(monkeypatch: pytest.MonkeyPatch, encoding: str) -> None:
    """Text files opened without an encoding use `encoding` — the ANSI
    code page a Windows station hands Python. Both seams are patched for
    the reason given in `_break_writes_under`."""
    for module, real in ((io, io.open), (builtins, builtins.open)):
        def opener(file, mode="r", *args, _real=real, **kwargs):  # type: ignore[no-untyped-def]
            if "b" not in mode and not args and "encoding" not in kwargs:
                kwargs["encoding"] = encoding
            return _real(file, mode, *args, **kwargs)

        monkeypatch.setattr(module, "open", opener)


def test_a_cyrillic_nickname_is_stored_on_a_western_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a station whose code page is cp1252 a Cyrillic nickname cannot
    be written in that code page. The registration was already changed
    in memory, so the manager answered 500 while its own list showed the
    change: the till never re-read the table, and the printer was gone
    again after the manager restarted."""
    _default_text_encoding(monkeypatch, "cp1252")
    path = tmp_path / "printers.json"
    _registry_with_one_printer(path, nickname="Принтер чеків")

    reloaded = PrinterRegistry(path=path)
    reloaded.load()

    assert [r.nickname for r in reloaded.all_registrations()] == ["Принтер чеків"]


def test_a_file_written_in_the_stations_code_page_is_still_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Up to this version printers.json was written in the station's
    code page. Such a file is on the tills now and has to be read, not
    refused: refused, it counts as no printers and the next register()
    writes over the shop's others."""
    monkeypatch.setattr(station_text, "station_encoding", lambda: "cp1251")
    path = tmp_path / "printers.json"
    registry = _registry_with_one_printer(path, nickname="Бар")
    entry = registry.all_registrations()[0].model_dump(mode="json")
    entry["nickname"] = "Бар «Чорна кава»"
    path.write_bytes(
        json.dumps({"printers": [entry]}, ensure_ascii=False).encode("cp1251")
    )

    reloaded = PrinterRegistry(path=path)
    reloaded.load()

    assert [r.nickname for r in reloaded.all_registrations()] == ["Бар «Чорна кава»"]


def test_a_cyrillic_nickname_round_trips_through_save_and_load(
    tmp_path: Path,
) -> None:
    """`save()` writes UTF-8 and `load()` reads UTF-8 first, so the
    nickname comes back the way the operator typed it."""
    path = tmp_path / "printers.json"
    _registry_with_one_printer(path, nickname="Бар «Чорна кава»")

    reloaded = PrinterRegistry(path=path)
    reloaded.load()

    assert len(reloaded.all_registrations()) == 1
    assert reloaded.all_registrations()[0].nickname == "Бар «Чорна кава»"
    assert "Бар «Чорна кава»" in path.read_bytes().decode("utf-8")


def _saves_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every write of the registry file fails before it reaches the disk:
    a full disk, a read-only folder, an antivirus lock on the rename."""
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("the disk refused the write")

    monkeypatch.setattr(os, "replace", refuse)


def test_a_register_that_cannot_be_stored_changes_nothing_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A change the disk refused must not show in the list: the manager
    answers an error, and a list that still showed the change would lose
    it silently on the next restart."""
    registry = _registry_with_one_printer(tmp_path / "printers.json")
    before = [r.model_dump() for r in registry.all_registrations()]
    second = registry.add_manual_descriptor(host="10.0.0.13", port=9100)
    first_id = registry.all_registrations()[0].descriptor.id
    _saves_fail(monkeypatch)

    with pytest.raises(OSError):
        registry.register(RegistrationRequest(
            id=second.id, kind=PrinterKind.label, nickname="Етикетки",
        ))
    with pytest.raises(OSError):
        registry.register(RegistrationRequest(
            id=first_id, kind=PrinterKind.label, nickname="Інша роль",
        ))

    assert [r.model_dump() for r in registry.all_registrations()] == before


def test_an_unregister_that_cannot_be_stored_keeps_the_printer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry_with_one_printer(tmp_path / "printers.json")
    printer_id = registry.all_registrations()[0].descriptor.id
    _saves_fail(monkeypatch)

    with pytest.raises(OSError):
        registry.unregister(printer_id)

    assert [r.descriptor.id for r in registry.all_registrations()] == [printer_id]


def test_a_file_with_a_byte_order_mark_is_read(tmp_path: Path) -> None:
    """Notepad saves UTF-8 with a byte order mark. Refused, the file
    counts as no printers and the next register() writes over them."""
    path = tmp_path / "printers.json"
    _registry_with_one_printer(path, nickname="Бар")
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    reloaded = PrinterRegistry(path=path)
    reloaded.load()

    assert [r.nickname for r in reloaded.all_registrations()] == ["Бар"]


def test_an_unreadable_file_is_kept_aside_before_the_next_save(
    tmp_path: Path,
) -> None:
    """A file this version cannot read is copied aside on load, so the
    next register(), which writes only what is in memory, does not
    destroy the shop's printers for good."""
    path = tmp_path / "printers.json"
    broken = b'{"printers": [ {"nickname": "half'
    path.write_bytes(broken)
    registry = PrinterRegistry(path=path)
    registry.load()
    descriptor = registry.add_manual_descriptor(host="10.0.0.12", port=9100)
    registry.register(RegistrationRequest(
        id=descriptor.id, kind=PrinterKind.receipt, nickname="Bar",
    ))

    kept = [p.read_bytes() for p in tmp_path.glob("printers.json.unreadable-*")]
    assert kept == [broken]


def test_an_unreadable_file_that_cannot_be_kept_aside_is_never_written_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "printers.json"
    broken = b'{"printers": [ {"nickname": "half'
    path.write_bytes(broken)

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("the folder is read-only")

    monkeypatch.setattr("src.devices.station_text.shutil.copyfile", refuse)
    registry = PrinterRegistry(path=path)
    registry.load()
    descriptor = registry.add_manual_descriptor(host="10.0.0.12", port=9100)

    with pytest.raises(OSError):
        registry.register(RegistrationRequest(
            id=descriptor.id, kind=PrinterKind.receipt, nickname="Bar",
        ))

    assert path.read_bytes() == broken
    assert registry.all_registrations() == []
