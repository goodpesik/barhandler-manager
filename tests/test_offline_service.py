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

    for i in range(int(os.environ.get("FAKE_SPAM", "0"))):
        print("line %05d " % i + "x" * 80, flush=True)
    time.sleep(float(os.environ.get("FAKE_HEALTH_DELAY", "0")))
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
        await asyncio.gather(task, return_exceptions=True)


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
        await asyncio.gather(task, return_exceptions=True)


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
        await asyncio.gather(task, return_exceptions=True)


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
        await asyncio.gather(task, return_exceptions=True)


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
        await asyncio.gather(task, return_exceptions=True)


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
        await asyncio.gather(task, return_exceptions=True)


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
    ("installers/install.sh", 'pkill', "-f device-handler-offline"),
    ("installers/install.ps1", "Stop-Process -Id $p.ProcessId", 'Stop-Process -Name "device-handler-offline"'),
    ("installers/mac-postinstall.sh", "pkill -9", "-f device-handler-offline"),
    ("installers/install-android.sh", "pkill-main", "pkill -9 -f device-handler-offline|pkill -f device-handler-offline"),
    ("src/routes/system.py", "pkill -f \"BarhandlerManager.app", "pkill -f device-handler-offline"),
]


@pytest.mark.parametrize("path,manager,offline", _KILL_SITES, ids=[s[0] for s in _KILL_SITES])
def test_whatever_stops_the_manager_stops_the_offline_service(path, manager, offline):
    lines = (_ROOT / path).read_text(encoding="utf-8").splitlines()
    if manager == "pkill" or manager == "pkill -9":
        # The shell scripts stop the manager by its bundle path.
        managers = [l for l in lines if manager in l and "BarhandlerManager.app/Contents/MacOS/bhm" in l]
    elif manager == "pkill-main":
        # The Android install stops the manager by its main.py.
        managers = [l for l in lines if l.lstrip().startswith("pkill") and "main.py" in l]
    else:
        managers = [l for l in lines if manager in l]
    offlines = [l for l in lines if any(o in l for o in offline.split("|"))]
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
        await asyncio.gather(task, return_exceptions=True)


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
        await asyncio.gather(task, return_exceptions=True)



# ---- review round 1 (PET-971) -------------------------------------------


@pytest.mark.asyncio
async def test_stopped_while_checking_the_port_starts_nothing(fake):
    make, configs = fake
    import time as _t

    def slow_fetch(port):
        _t.sleep(0.4)
        return None

    from src.services import offline_service as mod

    spawned = []
    real = mod.asyncio.create_subprocess_exec

    async def spy(*args, **kw):
        spawned.append(args)
        return await real(*args, **kw)

    monkeypatch_attr = pytest.MonkeyPatch()
    monkeypatch_attr.setattr(mod.asyncio, "create_subprocess_exec", spy)
    try:
        svc = make(fetch_health=slow_fetch)
        task = asyncio.create_task(svc.run_forever())
        await asyncio.sleep(0.1)  # inside the port check
        await svc.stop()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.5)
        # Not even started: the port check is where it stops.
        assert spawned == []
        assert configs() == []
        assert svc._proc is None
    finally:
        monkeypatch_attr.undo()


@pytest.mark.asyncio
async def test_stopped_while_the_child_is_being_spawned_does_not_keep_it(fake, monkeypatch):
    make, configs = fake
    from src.services import offline_service as mod

    real = mod.asyncio.create_subprocess_exec
    children = []

    async def slow_spawn(*args, **kw):
        proc = await real(*args, **kw)
        children.append(proc)
        await asyncio.sleep(0.3)  # the stop lands here
        return proc

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", slow_spawn)
    svc = make()
    task = asyncio.create_task(svc.run_forever())
    assert await _until(lambda: children)
    await svc.stop()
    await asyncio.gather(task, return_exceptions=True)
    await children[0].wait()
    # Stopped before the config went in: nothing ever served, nothing left.
    assert children[0].returncode is not None
    assert configs() == []


@pytest.mark.asyncio
async def test_stopped_while_waiting_for_health_leaves_no_child(fake, monkeypatch):
    make, configs = fake
    monkeypatch.setenv("FAKE_HEALTH_DELAY", "5")
    svc = make()
    task = asyncio.create_task(svc.run_forever())
    assert await _until(lambda: svc._proc is not None and len(configs()) == 1)
    proc = svc._proc
    await svc.stop()
    outcome = (await asyncio.gather(task, return_exceptions=True))[0]
    assert not isinstance(outcome, Exception), outcome
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_cancelling_the_supervisor_stops_the_child(fake):
    make, _ = fake
    svc = make()
    task = asyncio.create_task(svc.run_forever())
    assert await _until(lambda: svc.state.running)
    proc = svc._proc
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_a_stray_service_of_ours_is_stopped_and_replaced(fake):
    make, configs = fake
    import subprocess as sp

    stray = sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        answers = [{"name": SERVICE_NAME, "version": "0.0.1", "pid": stray.pid}]

        def fetch(port):
            if answers:
                if stray.poll() is None:
                    return answers[0]
                answers.clear()
            from src.services.offline_service import _fetch_health
            return _fetch_health(port)

        svc = make(fetch_health=fetch)
        task = asyncio.create_task(svc.run_forever())
        try:
            assert await _until(lambda: svc.state.running, timeout=15)
            assert stray.wait(timeout=5) is not None
            assert len(configs()) == 1
        finally:
            await svc.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        if stray.poll() is None:
            stray.kill()


@pytest.mark.asyncio
async def test_another_program_on_the_port_is_never_killed(fake):
    make, _ = fake
    import subprocess as sp

    other = sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        svc = make(fetch_health=lambda p: {"name": "someone-else", "pid": other.pid})
        task = asyncio.create_task(svc.run_forever())
        assert await _until(lambda: svc.state.last_error is not None)
        await svc.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert other.poll() is None
    finally:
        other.kill()


@pytest.mark.asyncio
async def test_the_service_log_is_capped_while_it_runs(fake, monkeypatch, tmp_path):
    make, _ = fake
    monkeypatch.setattr("src.services.offline_service.LOG_MAX_BYTES", 20_000)
    monkeypatch.setenv("FAKE_SPAM", "600")  # ~55 KB of output
    svc = make()
    task = asyncio.create_task(svc.run_forever())
    try:
        assert await _until(lambda: svc.state.running)
        log_file = tmp_path / "service.log"
        assert await _until(lambda: (tmp_path / "service.log.1").exists())
        assert log_file.stat().st_size <= 20_000
    finally:
        await svc.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_missing_runtime_is_not_counted_as_a_restart(tmp_path):
    svc = OfflineService(
        lambda: {"appid": "shop"},
        argv=[str(tmp_path / "no-node"), str(tmp_path / "no-main.js")],
        expected_version="1.2.3",
        sleep=_fast_sleep,
        log_path=tmp_path / "service.log",
    )
    task = asyncio.create_task(svc.run_forever())
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert "runtime missing" in svc.state.last_error
    assert svc.state.restarts == 0


def test_no_console_window_for_the_child_on_windows(monkeypatch):
    from src.services import offline_service as mod

    monkeypatch.setattr(mod.sys, "platform", "win32")
    assert mod._no_window() == {"creationflags": 0x08000000}
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    assert mod._no_window() == {}



def test_every_installer_that_stops_the_manager_is_checked_above():
    """A new install/update script must join the table, not slip past it."""
    import re

    listed = {path for path, _, _ in _KILL_SITES}
    kills = re.compile(r"^\s*(pkill|taskkill|Exec\('cmd\.exe', '/c taskkill|Filename: \"\{cmd\}\"; Parameters: \"/c taskkill|Stop-Process)")
    for path in sorted((_ROOT / "installers").iterdir()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(kills.search(line) for line in text.splitlines()):
            assert f"installers/{path.name}" in listed, path.name


def test_linux_would_not_find_the_service_by_its_short_name():
    """The kernel keeps 15 characters of a process name: `pkill -x` with the
    full name is a no-op on Linux, so the shell scripts must use -f."""
    for path in ("installers/install.sh", "installers/install-android.sh", "installers/mac-postinstall.sh", "src/routes/system.py"):
        assert "-x device-handler-offline" not in (_ROOT / path).read_text(encoding="utf-8"), path



def test_the_runtime_lies_beside_the_frozen_manager_not_inside_it(monkeypatch):
    """Inside the one-file build it would be unpacked on every start."""
    from src.services import offline_service as mod

    monkeypatch.setattr(mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod.sys, "executable", "C:/Users/a/AppData/Local/DeviceHandler/bhm.exe")
    assert mod.runtime_dir("petshandler").as_posix().endswith("DeviceHandler/offline-runtime/petshandler")

    monkeypatch.setattr(mod.sys, "platform", "darwin")
    monkeypatch.setattr(mod.sys, "executable", "/Applications/Device Handler.app/Contents/MacOS/bhm")
    assert mod.runtime_dir("petshandler") == Path(
        "/Applications/Device Handler.app/Contents/Resources/offline-runtime/petshandler"
    )
