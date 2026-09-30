"""BH-183 — the Settings test print keeps the printer's error code.

/print/* answered a failed job with {code, message}; the /devices test-print
and probe-codepage routes answered with a plain string, so the till could not
tell paper, cover or a render failure from any other failure on exactly the
screen where a printer gets checked.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.devices.printer import PrintRenderError, PrinterUnavailable


class _FailingDevice:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def is_connected(self) -> bool:
        return True

    async def enqueue(self, _job):
        raise self.exc


def _register(client, auth_headers, monkeypatch, protocol: str) -> str:
    from src.devices import scan
    from src.models.printer import PrinterDescriptor, PrinterTransport

    desc = PrinterDescriptor(
        id="net:10.0.0.50", transport=PrinterTransport.network,
        label="Test", host="10.0.0.50", port=9100,
    )
    monkeypatch.setattr(scan, "discover_network", lambda **_: [desc])
    client.post("/devices/discover", headers=auth_headers)
    r = client.post(
        "/devices/register", headers=auth_headers,
        json={"id": desc.id, "kind": "receipt", "paper_width": 80, "protocol": protocol},
    )
    assert r.status_code == 200, r.text
    return desc.id


def _fail_with(client, monkeypatch, exc: Exception) -> None:
    registry = client.app.state.registry

    async def _device(_pid):
        return _FailingDevice(exc)

    monkeypatch.setattr(registry, "get_device", _device)


@pytest.mark.parametrize(
    ("path", "protocol"),
    [("test-print", "escpos"), ("test-print", "tspl"), ("probe-codepage", "escpos")],
)
@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (PrintRenderError("render failed: cannot open resource"), "render_failed"),
        (PrinterUnavailable("XP-58: paper is out", code="out_of_paper"), "out_of_paper"),
        (PrinterUnavailable("gone"), "unavailable"),
    ],
)
def test_a_failed_job_keeps_its_code(
    client, auth_headers, monkeypatch, path, protocol, exc, code,
) -> None:
    pid = _register(client, auth_headers, monkeypatch, protocol)
    _fail_with(client, monkeypatch, exc)

    r = client.post(f"/devices/{pid}/{path}", headers=auth_headers)

    assert r.status_code == 503
    assert r.json()["detail"] == {"code": code, "message": str(exc)}


def test_any_other_failure_still_answers_503_with_its_text(
    client, auth_headers, monkeypatch,
) -> None:
    pid = _register(client, auth_headers, monkeypatch, "escpos")
    _fail_with(client, monkeypatch, RuntimeError("boom"))

    r = client.post(f"/devices/{pid}/test-print", headers=auth_headers)

    assert r.status_code == 503
    assert r.json()["detail"] == "boom"


def test_every_job_in_devices_routes_keeps_the_code() -> None:
    src = Path("src/routes/devices.py").read_text(encoding="utf-8")
    jobs = len(re.findall(r"await device\.enqueue\(", src))
    kept = len(re.findall(r"except PrinterUnavailable as exc:", src))
    assert jobs == 3
    assert kept == jobs
