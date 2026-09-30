"""PET-971 — the manager runs the Petshandler offline service and watches it.

A real child process stands in for the Node service: it reads its config as
the first line of stdin, answers /health on the loopback and stops when stdin
ends — the contract the real service keeps (petshandler-offline, watchParent).
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import textwrap
from pathlib import Path

import pytest

from src.services.offline_service import OfflineService, SERVICE_NAME

FAKE = textwrap.dedent(
    """
    import json, os, sys, threading, time
    from http.server import BaseHTTPRequestHandler, HTTPServer

    cfg = json.loads(sys.stdin.readline())
    with open(os.environ["FAKE_SEEN"], "a") as f:
        f.write(json.dumps(cfg) + "\\n")
    name = os.environ.get("FAKE_NAME", "petshandler-offline")
    version = os.environ.get("FAKE_VERSION", "1.2.3")
    die_after = float(os.environ.get("FAKE_DIE_AFTER", "0"))

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"name": name, "version": version}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", cfg["port"]), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if die_after:
        time.sleep(die_after)
        os._exit(3)
    sys.stdin.read()          # until the manager closes the pipe
    srv.shutdown()
    sys.exit(0)
    """
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _fast_sleep(seconds: float) -> None:
    # The real waits are seconds to minutes; the order of events is what matters.
    await asyncio.sleep(min(seconds, 0.05))


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_service.py"
    script.write_text(FAKE)
    seen = tmp_path / "seen.jsonl"
    monkeypatch.setenv("FAKE_SEEN", str(seen))

    def make(config=None, **kw):
        cfg = {"appid": "shop", "deviceToken": "pho_secret", "dataKey": "k"} if config is None else config
        return OfflineService(
            lambda: cfg,
            argv=[sys.executable, str(script)],
            expected_version=kw.pop("expected", "1.2.3"),
            port=kw.pop("port", _free_port()),
            log_path=tmp_path / "service.log",
            sleep=_fast_sleep,
            **kw,
        )

    def configs():
        if not seen.exists():
            return []
        return [json.loads(line) for line in seen.read_text().splitlines()]

    return make, configs


async def _until(pred, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_starts_with_the_config_on_stdin_and_reports_our_version(fake):
    make, configs = fake
    svc = make()
    task = asyncio.create_task(svc.run_forever())
    try:
        assert await _until(lambda: svc.state.running)
        assert svc.state.version == "1.2.3"
        seen = configs()
        assert len(seen) == 1
        # The secrets travel on stdin, together with the port to listen on.
        assert seen[0]["deviceToken"] == "pho_secret"
        assert seen[0]["port"] == svc._port
    finally:
        await svc.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_stop_closes_stdin_and_the_service_exits_on_its_own(fake):
    make, _ = fake
    svc = make()
    task = asyncio.create_task(svc.run_forever())
    try:
        assert await _until(lambda: svc.state.running)
        proc = svc._proc
        await svc.stop()
        # Exit code 0: it left through its own stdin-closed path, not a kill.
        assert proc.returncode == 0
        assert not svc.state.running
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_a_service_that_dies_is_started_again(fake, monkeypatch):
    make, configs = fake
    monkeypatch.setenv("FAKE_DIE_AFTER", "0.3")
    svc = make()
    task = asyncio.create_task(svc.run_forever())
    try:
        assert await _until(lambda: len(configs()) >= 2, timeout=15)
        assert svc.state.restarts >= 1
        assert "exited" in (svc.state.last_error or "")
    finally:
        await svc.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_another_version_answering_is_not_ours(fake, monkeypatch):
    make, _ = fake
    monkeypatch.setenv("FAKE_VERSION", "0.9.0")
    svc = make()
    task = asyncio.create_task(svc.run_forever())
    try:
        assert await _until(lambda: svc.state.last_error is not None)
        assert not svc.state.running
        assert "0.9.0" in svc.state.last_error and "1.2.3" in svc.state.last_error
    finally:
        await svc.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_a_shop_not_activated_starts_nothing(fake):
    make, configs = fake
    svc = make(config={})
    task = asyncio.create_task(svc.run_forever())
    try:
        await asyncio.sleep(0.5)
        assert configs() == []
        assert svc._proc is None
    finally:
        await svc.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_a_port_held_by_another_program_is_left_alone(fake):
    make, configs = fake
    port = _free_port()

    svc = make(port=port, fetch_health=lambda p: {"name": "someone-else", "version": "9"})
    task = asyncio.create_task(svc.run_forever())
    try:
        assert await _until(lambda: svc.state.last_error is not None)
        await asyncio.sleep(0.3)
        assert configs() == []
        assert "taken by someone-else" in svc.state.last_error
    finally:
        await svc.stop()
        task.cancel()


def test_the_runtime_is_shipped_under_its_own_process_name():
    from src.services.offline_service import NODE_PROCESS_NAME, node_path

    assert NODE_PROCESS_NAME == "device-handler-offline"
    assert node_path(Path("/x")).name.startswith(NODE_PROCESS_NAME)
    assert SERVICE_NAME == "petshandler-offline"


# Every place that stops the manager stops the offline service too: a running
# exe locks its file on Windows, and a stray one would hold the port.
_ROOT = Path(__file__).resolve().parent.parent
_KILL_SITES = [
    # (file, a line that stops the manager, a line that stops the offline service)
    ("installers/barhandler-setup.iss", "taskkill /F /IM {#MyAppExeAltName}", "taskkill /F /IM device-handler-offline.exe"),
    ("installers/install.sh", 'pkill', "-x device-handler-offline"),
    ("installers/install.ps1", "Stop-Process -Id $p.ProcessId", 'Stop-Process -Name "device-handler-offline"'),
    ("installers/mac-postinstall.sh", "pkill -9", "-x device-handler-offline"),
    ("src/routes/system.py", "pkill -f \"BarhandlerManager.app", "pkill -x device-handler-offline"),
]


@pytest.mark.parametrize("path,manager,offline", _KILL_SITES, ids=[s[0] for s in _KILL_SITES])
def test_whatever_stops_the_manager_stops_the_offline_service(path, manager, offline):
    lines = (_ROOT / path).read_text(encoding="utf-8").splitlines()
    if manager == "pkill" or manager == "pkill -9":
        # The shell scripts stop the manager by its bundle path.
        managers = [l for l in lines if manager in l and "BarhandlerManager.app/Contents/MacOS/bhm" in l]
    else:
        managers = [l for l in lines if manager in l]
    offlines = [l for l in lines if offline in l]
    assert managers, f"{path}: the manager's stop lines moved — update this test"
    assert len(offlines) == len(managers), (path, len(managers), len(offlines))


def _lifespan(config, monkeypatch, runtime_present: bool):
    """Run the manager's lifespan once and report what the offline part did."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from src.server import create_app
    from src.services import offline_service

    events: list[str] = []

    async def run_forever(self):
        events.append("run")

    async def stop(self):
        events.append("stop")

    monkeypatch.setattr(offline_service.OfflineService, "run_forever", run_forever)
    monkeypatch.setattr(offline_service.OfflineService, "stop", stop)
    monkeypatch.setattr(
        offline_service, "node_path",
        lambda root=None: Path(__file__) if runtime_present else Path("/nonexistent/node"),
    )
    with patch("src.devices.scan.discover_usb", return_value=[]), \
         patch("src.devices.scan.discover_network", return_value=[]), \
         patch("src.devices.scan.discover_bluetooth", return_value=[]):
        with TestClient(create_app(config)) as c:
            has_service = hasattr(c.app.state, "offline_service")
    return events, has_service


def test_a_build_with_the_runtime_runs_the_offline_service(config, monkeypatch):
    events, has_service = _lifespan(config, monkeypatch, runtime_present=True)
    assert has_service
    # Started with the manager, stopped with it.
    assert events == ["run", "stop"]


def test_a_build_without_the_runtime_leaves_it_alone(config, monkeypatch):
    events, has_service = _lifespan(config, monkeypatch, runtime_present=False)
    assert not has_service
    assert events == []


@pytest.mark.asyncio
async def test_a_new_activation_restarts_the_service_with_it(fake, monkeypatch):
    make, configs = fake
    monkeypatch.setattr("src.services.offline_service.HEALTH_EVERY_SEC", 0.01)
    current = {"appid": "shop", "deviceToken": "pho_one", "dataKey": "k"}
    svc = make(config=current)
    svc._load_config = lambda: dict(current)
    task = asyncio.create_task(svc.run_forever())
    try:
        assert await _until(lambda: svc.state.running)
        current["deviceToken"] = "pho_two"
        assert await _until(lambda: len(configs()) >= 2 and svc.state.running)
        assert configs()[-1]["deviceToken"] == "pho_two"
        # A change of activation is not a failure.
        assert svc.state.last_error is None
    finally:
        await svc.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_switching_off_stops_it_without_calling_it_a_failure(fake):
    make, configs = fake
    current = {"cfg": {"appid": "shop", "deviceToken": "pho_one", "dataKey": "k"}}
    svc = make(config={})
    svc._load_config = lambda: current["cfg"]
    task = asyncio.create_task(svc.run_forever())
    try:
        assert await _until(lambda: svc.state.running)
        current["cfg"] = None
        await svc.stop_service()
        await asyncio.sleep(0.5)
        assert not svc.state.running
        assert svc.state.last_error is None
        assert len(configs()) == 1
    finally:
        await svc.stop()
        task.cancel()
