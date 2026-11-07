"""PrinterRegistry persistence — PET-1304.

Only what this ticket is about: the shop's printers survive a save
that does not finish, and a Cyrillic nickname comes back the way the
operator typed it. The rest of the registry's behaviour is covered by
`test_discover_and_register.py` and `test_tspl_receipt.py`.
"""

from __future__ import annotations

import builtins
import io
import os
from pathlib import Path

import pytest

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


def test_a_cyrillic_nickname_round_trips_through_save_and_load(
    tmp_path: Path,
) -> None:
    """`save()` and `load()` have to agree on the encoding. They do by
    using none at all — the station's own code page on both sides. An
    explicit one on the writing side only is what review found in the
    terminal registry: «Каса 1» came back as mojibake on a Ukrainian
    Windows, and on a Western one the read raised and the file counted
    as no printers, after which the next save overwrote the lot."""
    path = tmp_path / "printers.json"
    _registry_with_one_printer(path, nickname="Бар «Чорна кава»")

    reloaded = PrinterRegistry(path=path)
    reloaded.load()

    assert len(reloaded.all_registrations()) == 1
    assert reloaded.all_registrations()[0].nickname == "Бар «Чорна кава»"
