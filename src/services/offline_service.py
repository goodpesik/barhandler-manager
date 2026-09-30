"""PET-971 — the Petshandler offline service, run and watched by the manager.

The offline mode of Petshandler is a Node service (petshandler-offline) that
serves the offline build of the app on 127.0.0.1:9898 and answers its API from
a local copy of the shop's data. The manager ships the Node runtime and the
service with it and is the one that keeps it alive:

* it starts the service with its config written as ONE JSON line on stdin —
  the device token and the data key never appear in the process list or the
  environment — and keeps stdin open: the service stops by itself when the
  pipe ends, i.e. when the manager quits, crashes or is killed by an update;
* it checks /health for the EXACT version it ships (the rule from
  install.sh: «something answers» is not «our version answers»);
* it restarts the service when it dies or stops answering, waiting longer
  after each failure, and stops it on shutdown.

Where the config comes from (activation, the token, the key kept by the OS)
is PET-972. Until a shop is activated nothing is started.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from src.config import APP_DIR

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class OfflineProduct:
    """A product whose till can work offline through this manager."""

    #: The service's own name in /health: a different program on the port is not ours.
    service_name: str
    #: The loopback port its offline service listens on.
    port: int
    #: The only servers a till of this product may sync with. The manager's
    #: API key is public (it ships in the web apps), so an activation must not
    #: be able to point a shop's data at any other host.
    api_hosts: tuple[str, ...]


#: PET-971 — the products the manager can run an offline service for. Only
#: Petshandler for now; FitStudio and BarHandler are planned (BarHandler on
#: Android especially) and join here with their own service, port and
#: runtime folder, while the supervision stays the same.
PRODUCTS: dict[str, OfflineProduct] = {
    "petshandler": OfflineProduct(
        service_name="petshandler-offline",
        port=9898,
        api_hosts=("api.petshandler.com", "api-dev.petshandler.com"),
    ),
}
DEFAULT_PRODUCT = "petshandler"
SERVICE_NAME = PRODUCTS[DEFAULT_PRODUCT].service_name
DEFAULT_PORT = PRODUCTS[DEFAULT_PRODUCT].port
#: The Node runtime is shipped under this name so install, update and
#: uninstall scripts can find and stop it (a bare «node» could be anybody's).
NODE_PROCESS_NAME = "device-handler-offline"

#: How often a running service is asked /health.
HEALTH_EVERY_SEC = 10.0
#: Missed answers in a row before the service is restarted.
HEALTH_MISSES = 3
#: How long a fresh start may take to answer /health.
START_TIMEOUT_SEC = 30.0
#: Waits between restarts after failures; the last one repeats.
BACKOFF_SEC = (2.0, 5.0, 15.0, 30.0, 60.0)
#: A service up this long is healthy again: the next failure starts the waits over.
STABLE_AFTER_SEC = 300.0
#: How often to look again while the shop is not activated.
IDLE_EVERY_SEC = 30.0
#: The service's own output is kept, capped, for support.
LOG_MAX_BYTES = 5 * 1024 * 1024


#: The runtime folder's name; the shops' data is in APP_DIR/offline.
RUNTIME_FOLDER = "offline-runtime"

#: The code root: `_MEIPASS` in a frozen build, the checkout from source.
_CODE_ROOT = Path(__file__).resolve().parents[2]


def runtime_dir(product: str = DEFAULT_PRODUCT) -> Path:
    """Where a product's Node runtime, service and offline app are.

    NOT inside the one-file build: that is unpacked into a temporary folder
    on every start, and ~150 MB of Node and app would be unpacked with it
    each time. The installers lay it down next to the manager instead:

    * Windows — `{app}\\offline-runtime\\<product>` beside bhm.exe (Inno Setup);
    * macOS — `Device Handler.app/Contents/Resources/offline-runtime/<product>`,
      signed with the app (scripts/mac_sign_and_package.sh);
    * from source — `offline-runtime/<product>/` in the checkout, by hand.

    Read only in every case, and apart from the shops' data in
    APP_DIR/offline (offline_state): on Windows APP_DIR is the install
    folder itself, and an update replaces the runtime wholesale — it must
    never touch the queue of operations waiting for the server.
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        if sys.platform == "darwin":
            # …/Device Handler.app/Contents/MacOS/bhm → Contents/Resources
            return exe.parents[1] / "Resources" / RUNTIME_FOLDER / product
        return exe.parent / RUNTIME_FOLDER / product
    return _CODE_ROOT / RUNTIME_FOLDER / product


def node_path(root: Optional[Path] = None) -> Path:
    root = root or runtime_dir()
    exe = f"{NODE_PROCESS_NAME}.exe" if sys.platform == "win32" else NODE_PROCESS_NAME
    return root / exe


def service_script(root: Optional[Path] = None) -> Path:
    return (root or runtime_dir()) / "service" / "main.js"


def shipped_version(root: Optional[Path] = None) -> Optional[str]:
    """The service version packed with this manager (service/version.txt)."""
    try:
        # Not «VERSION»: that name is the manager's own version (src/version.py).
        text = ((root or runtime_dir()) / "service" / "version.txt").read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None


def _fetch_health(port: int, timeout: float = 2.0) -> Optional[dict]:
    """GET /health on the loopback; None when nothing sensible answers."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — refused, timeout, not JSON: all «no»
        return None
    return body if isinstance(body, dict) else None


@dataclass
class OfflineServiceState:
    """What the dashboard and the menu (PET-972) show."""

    running: bool = False
    pid: Optional[int] = None
    version: Optional[str] = None
    restarts: int = 0
    last_error: Optional[str] = None
    started_at: Optional[float] = None
    extra: dict = field(default_factory=dict)


class OfflineService:
    """Starts the offline service, keeps it on our version, restarts it."""

    def __init__(
        self,
        load_config: Callable[[], Optional[dict]],
        *,
        argv: Optional[list[str]] = None,
        expected_version: Optional[str] = None,
        port: int = DEFAULT_PORT,
        log_path: Optional[Path] = None,
        fetch_health: Callable[[int], Optional[dict]] = _fetch_health,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._load_config = load_config
        root = runtime_dir()
        self._argv = argv or [str(node_path(root)), str(service_script(root))]
        self._expected = expected_version if expected_version is not None else shipped_version(root)
        self._port = port
        self._log_path = log_path or (APP_DIR / "offline" / "service.log")
        self._fetch = fetch_health
        self._sleep = sleep
        self._proc: Optional[asyncio.subprocess.Process] = None
        # PET-972 — the config the running service was started with: a new
        # activation or a switch-off restarts it.
        self._cfg_key: Optional[str] = None
        self._stopped_on_purpose = False
        self._pump: Optional[asyncio.Task] = None
        self._stopping = False
        self._failures = 0
        self.state = OfflineServiceState()

    # ---- public ------------------------------------------------------

    async def run_forever(self) -> None:
        """The supervision loop, until stop() — or cancellation, which also
        stops the child rather than leaving it on the port."""
        try:
            await self._loop()
        except asyncio.CancelledError:
            await self._stop_process()
            raise

    async def _loop(self) -> None:
        told_idle = False
        while not self._stopping:
            cfg = self._load_config()
            if not cfg:
                if not told_idle:
                    log.info("offline service: the shop is not activated, nothing to start")
                    told_idle = True
                await self._sleep(IDLE_EVERY_SEC)
                continue
            told_idle = False
            if not self._runtime_ready():
                await self._sleep(IDLE_EVERY_SEC)
                continue
            self._cfg_key = _config_key(cfg)
            started = await self._start(cfg)
            if started:
                await self._watch()
            if self._stopping:
                break
            await self._stop_process()
            await self._sleep(self._next_wait())

    async def stop(self) -> None:
        """Stop supervising and stop the service (manager shutdown)."""
        self._stopping = True
        await self._stop_process()

    async def stop_service(self) -> None:
        """Stop the running service now (switching offline mode off); the loop
        goes on and starts nothing until a shop is activated again."""
        self._stopped_on_purpose = True
        await self._stop_process()

    async def refresh(self) -> Optional[dict]:
        """Ask /health now; the answer, when it is our service, or None."""
        health = await asyncio.to_thread(self._fetch, self._port)
        if not self._ours(health):
            return None
        self._remember(health)
        return health

    # ---- internals ---------------------------------------------------

    def _runtime_ready(self) -> bool:
        missing = [p for p in self._argv[:2] if not Path(p).exists()]
        if missing:
            self._fail(f"runtime missing: {', '.join(missing)}", level=logging.ERROR, once=True, started=False)
            return False
        if not self._expected:
            self._fail("the shipped service has no version.txt", level=logging.ERROR, once=True, started=False)
            return False
        return True

    async def _start(self, cfg: dict) -> bool:
        other = await asyncio.to_thread(self._fetch, self._port)
        if self._stopping:
            return False
        if other is not None:
            if other.get("name") == SERVICE_NAME and await self._stop_stray(other):
                pass
            else:
                # A different program holds the port: do not start a second
                # service into it.
                self._fail(
                    f"port {self._port} is taken by {other.get('name')} {other.get('version')}"
                    f" (want {SERVICE_NAME} {self._expected})",
                    started=False,
                )
                return False
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=_child_env(),
                **_no_window(),
            )
        except OSError as e:
            self._fail(f"could not start: {e}", level=logging.ERROR)
            return False
        self._proc = proc
        self._pump = asyncio.create_task(self._copy_output(proc), name="offline-service-log")
        if self._stopping:
            # Stopped while the child was being spawned: it must not outlive us.
            await self._stop_process()
            return False
        line = json.dumps({**cfg, "port": self._port}) + "\n"
        try:
            assert proc.stdin is not None
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()
        except (ConnectionError, AssertionError) as e:
            self._fail(f"could not pass the config: {e}", level=logging.ERROR)
            return False
        # Waiting for the right version, not for any answer.
        deadline = time.monotonic() + START_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if self._stopping or self._proc is not proc:
                return False
            if proc.returncode is not None:
                self._fail(f"exited on start with code {proc.returncode}")
                return False
            health = await asyncio.to_thread(self._fetch, self._port)
            if self._stopping or self._proc is not proc:
                return False
            if self._ours(health):
                self.state = OfflineServiceState(
                    running=True,
                    pid=proc.pid,
                    version=health.get("version"),
                    restarts=self.state.restarts,
                    started_at=time.monotonic(),
                )
                self._remember(health)
                log.info(
                    "offline service %s started (pid %s, shop %s)",
                    self._expected, proc.pid, cfg.get("appid"),
                )
                return True
            if health is not None:
                self._fail(
                    f"answers as {health.get('name')} {health.get('version')},"
                    f" want {SERVICE_NAME} {self._expected}",
                )
                return False
            await self._sleep(0.5)
        self._fail(f"no answer on /health within {START_TIMEOUT_SEC:.0f}s")
        return False

    async def _stop_stray(self, health: dict) -> bool:
        """Our own service, left on the port by a manager that died hard: it
        is not ours to supervise (we do not hold its stdin), so it goes."""
        pid = health.get("pid")
        if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
            return False
        log.warning(
            "offline service: a stray %s %s (pid %s) holds the port, stopping it",
            SERVICE_NAME, health.get("version"), pid,
        )
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError) as e:
            log.warning("offline service: could not stop pid %s: %s", pid, e)
            return False
        for _ in range(20):
            await self._sleep(0.25)
            if await asyncio.to_thread(self._fetch, self._port) is None:
                return True
        return False

    async def _copy_output(self, proc: asyncio.subprocess.Process) -> None:
        """The child's output into service.log, capped: rotated as it grows,
        not only when the service restarts."""
        assert proc.stdout is not None
        out = self._open_log()
        written = out.tell() if out is not None else 0
        try:
            async for line in proc.stdout:
                if out is None:
                    continue
                if written + len(line) > LOG_MAX_BYTES:
                    out.close()
                    out = self._open_log(rotate=True)
                    written = 0
                    if out is None:
                        continue
                out.write(line)
                out.flush()
                written += len(line)
        except (OSError, ValueError) as e:
            log.warning("offline service: log copy stopped: %s", e)
        finally:
            if out is not None:
                out.close()

    async def _watch(self) -> None:
        misses = 0
        while not self._stopping:
            await self._sleep(HEALTH_EVERY_SEC)
            if self._stopping:
                return
            proc = self._proc
            if self._stopped_on_purpose:
                # Switched off from the dashboard or Petshandler: not a failure.
                self._stopped_on_purpose = False
                return
            if proc is None or proc.returncode is not None:
                self._fail(f"exited with code {proc.returncode if proc else '?'}")
                return
            if _config_key(self._load_config()) != self._cfg_key:
                log.info("offline service: the activation changed, restarting")
                self._failures = 0
                return
            health = await asyncio.to_thread(self._fetch, self._port)
            if self._ours(health):
                self._remember(health)
                misses = 0
                started = self.state.started_at or time.monotonic()
                if self._failures and time.monotonic() - started >= STABLE_AFTER_SEC:
                    log.info("offline service stable again, restart waits reset")
                    self._failures = 0
                continue
            misses += 1
            if misses >= HEALTH_MISSES:
                self._fail(f"did not answer /health {misses} times in a row")
                return

    def _remember(self, health: dict) -> None:
        """What the dashboard shows and what the update guard asks."""
        self.state.extra = {
            "queued": health.get("queued"),
            "dataAsOf": health.get("dataAsOf"),
            "needsPairing": bool(health.get("needsPairing")),
            "appid": health.get("appid"),
        }

    def _ours(self, health: Optional[dict]) -> bool:
        return (
            health is not None
            and health.get("name") == SERVICE_NAME
            and health.get("version") == self._expected
        )

    async def _stop_process(self) -> None:
        proc, self._proc = self._proc, None
        pump, self._pump = self._pump, None
        self.state.running = False
        self.state.pid = None
        try:
            if proc is not None and proc.returncode is None:
                await self._end(proc)
        finally:
            if pump is not None:
                # The pipe closes with the process; the copy then ends by itself.
                await asyncio.gather(pump, return_exceptions=True)

    async def _end(self, proc: asyncio.subprocess.Process) -> None:
        # Closing stdin is the polite stop: the service shuts its server and
        # its database. Then SIGTERM, then SIGKILL.
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except (asyncio.TimeoutError, ConnectionError):
            pass
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3)
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError) as e:
                log.error("offline service pid %s could not be killed: %s", proc.pid, e)
        log.warning("offline service did not stop on its own and was terminated")

    def _next_wait(self) -> float:
        wait = BACKOFF_SEC[min(self._failures, len(BACKOFF_SEC)) - 1] if self._failures else BACKOFF_SEC[0]
        log.info("offline service: next start in %.0fs (failure %d)", wait, self._failures)
        return wait

    def _fail(
        self, why: str, *, level: int = logging.WARNING, once: bool = False, started: bool = True,
    ) -> None:
        if once and self.state.last_error == why:
            return
        self._failures += 1
        if started:
            # Shown as «restarts»: only a service that was really started.
            self.state.restarts += 1
        self.state.last_error = why
        log.log(level, "offline service: %s", why)

    def _open_log(self, rotate: bool = False):
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            if rotate or (self._log_path.exists() and self._log_path.stat().st_size > LOG_MAX_BYTES):
                self._log_path.replace(self._log_path.with_suffix(".log.1"))
            return open(self._log_path, "ab")
        except OSError as e:
            # The output is then dropped (read and discarded), never mixed
            # into the manager's own log.
            log.warning("offline service: no log file (%s)", e)
            return None


def _config_key(cfg: Optional[dict]) -> Optional[str]:
    return json.dumps(cfg, sort_keys=True) if cfg else None


def _no_window() -> dict:
    """Windows: no console window for the child (as for every process the
    manager starts, see src/routes/system.py)."""
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def _child_env() -> dict:
    """The child's environment: ours, minus the development config switch."""
    import os

    env = dict(os.environ)
    env.pop("OFFLINE_CONFIG", None)
    env["NODE_ENV"] = "production"
    return env
