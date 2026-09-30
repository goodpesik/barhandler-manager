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
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from src.config import APP_DIR

log = logging.getLogger(__name__)

#: The service's own name in /health: a different program on the port is not ours.
SERVICE_NAME = "petshandler-offline"
DEFAULT_PORT = 9898
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


#: The code root: `_MEIPASS` in a frozen build, the checkout from source.
_CODE_ROOT = Path(__file__).resolve().parents[2]


def runtime_dir() -> Path:
    """Where the Node runtime, the service and the offline app are.

    A packed resource, read only, addressed from the code like VERSION and
    the fonts (BH-158): in a frozen build that is `_MEIPASS/offline`. From
    source it is an `offline/` folder in the checkout, filled by hand.
    """
    return _CODE_ROOT / "offline"


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
        self._stopping = False
        self._failures = 0
        self.state = OfflineServiceState()

    # ---- public ------------------------------------------------------

    async def run_forever(self) -> None:
        """The supervision loop, until stop()."""
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
            self._fail(f"runtime missing: {', '.join(missing)}", level=logging.ERROR, once=True)
            return False
        if not self._expected:
            self._fail("the shipped service has no version.txt", level=logging.ERROR, once=True)
            return False
        return True

    async def _start(self, cfg: dict) -> bool:
        # Something already answers on the port. Ours from a previous run
        # would have stopped with its manager, so this is a stray or a
        # different program: do not start a second one into a busy port.
        other = await asyncio.to_thread(self._fetch, self._port)
        if other is not None:
            self._fail(
                f"port {self._port} is taken by {other.get('name')} {other.get('version')}"
                f" (want {SERVICE_NAME} {self._expected})",
            )
            return False
        out = self._open_log()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=out,
                stderr=out,
                env=_child_env(),
            )
        except OSError as e:
            self._fail(f"could not start: {e}", level=logging.ERROR)
            return False
        finally:
            if out is not None:
                out.close()
        line = json.dumps({**cfg, "port": self._port}) + "\n"
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()
        except (ConnectionError, AssertionError) as e:
            self._fail(f"could not pass the config: {e}", level=logging.ERROR)
            return False
        # Waiting for the right version, not for any answer.
        deadline = time.monotonic() + START_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if self._proc.returncode is not None:
                self._fail(f"exited on start with code {self._proc.returncode}")
                return False
            health = await asyncio.to_thread(self._fetch, self._port)
            if self._ours(health):
                self.state = OfflineServiceState(
                    running=True,
                    pid=self._proc.pid,
                    version=health.get("version"),
                    restarts=self.state.restarts,
                    started_at=time.monotonic(),
                )
                self._remember(health)
                log.info(
                    "offline service %s started (pid %s, shop %s)",
                    self._expected, self._proc.pid, cfg.get("appid"),
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
        self.state.running = False
        self.state.pid = None
        if proc is None or proc.returncode is not None:
            return
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
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        log.warning("offline service did not stop on its own and was terminated")

    def _next_wait(self) -> float:
        wait = BACKOFF_SEC[min(self._failures, len(BACKOFF_SEC)) - 1] if self._failures else BACKOFF_SEC[0]
        log.info("offline service: next start in %.0fs (failure %d)", wait, self._failures)
        return wait

    def _fail(self, why: str, *, level: int = logging.WARNING, once: bool = False) -> None:
        if once and self.state.last_error == why:
            return
        self._failures += 1
        self.state.restarts += 1
        self.state.last_error = why
        log.log(level, "offline service: %s", why)

    def _open_log(self):
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            if self._log_path.exists() and self._log_path.stat().st_size > LOG_MAX_BYTES:
                self._log_path.replace(self._log_path.with_suffix(".log.1"))
            return open(self._log_path, "ab")
        except OSError as e:
            log.warning("offline service: no log file (%s)", e)
            return None


def _config_key(cfg: Optional[dict]) -> Optional[str]:
    return json.dumps(cfg, sort_keys=True) if cfg else None


def _child_env() -> dict:
    """The child's environment: ours, minus the development config switch."""
    import os

    env = dict(os.environ)
    env.pop("OFFLINE_CONFIG", None)
    env["NODE_ENV"] = "production"
    return env
