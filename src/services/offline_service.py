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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from src.config import APP_DIR
from src.services.offline_secrets import SecretsUnreadable

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
    #: The path part of its server address: Petshandler's API lives under
    #: /api, FitStudioCRM's at the root of its host.
    api_path: str = "/api"
    #: Every offline device token of this product starts with this.
    token_prefix: str = "pho_"
    #: The folder in APP_DIR that holds this product's shops (one per shop).
    data_folder: str = "offline"
    #: The product's offline service keeps its own fiscal registers
    #: (Petshandler). A product without them fiscalises after the sync.
    fiscal: bool = True
    #: The till's receipts carry its number among the shop's computers
    #: (D<n>-0001), given at activation.
    numbered_devices: bool = False


#: PET-971 — the products the manager can run an offline service for. Each
#: has its own service, port, runtime folder and data folder, so the tills of
#: two products run side by side on one computer, while the supervision stays
#: the same. BarHandler (on Android especially) is planned.
PRODUCTS: dict[str, OfflineProduct] = {
    "petshandler": OfflineProduct(
        service_name="petshandler-offline",
        port=9898,
        api_hosts=("api.petshandler.com", "api-dev.petshandler.com"),
    ),
    # FC-798 — the FitStudioCRM till (fitstudiocrm-offline). No registers of
    # its own: offline sales are fiscalised by the server after the sync.
    "fitstudiocrm": OfflineProduct(
        service_name="fitstudiocrm-offline",
        port=9899,
        api_hosts=(
            "api.fitshandler.com",
            "api-dev.fitshandler.com",
            "api.fitstudiocrm.com",
            "api-dev.fitstudiocrm.com",
        ),
        api_path="",
        token_prefix="fso_",
        data_folder="offline-fitstudiocrm",
        fiscal=False,
        numbered_devices=True,
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

#: How the till's own service stands, beyond «running». `running` alone cannot
#: tell a start in progress from a start that will never finish: it is false
#: while the service comes up and false when it has just failed, so an online
#: app that has only that word has to call a starting till broken.
#: Nothing to start: no shop is activated for this product on this computer.
STARTUP_IDLE = "idle"
#: Coming up, and nothing has failed since the activation: switched on just
#: now, or the till number changed. It answers /health within
#: START_TIMEOUT_SEC or the phase becomes `failed`.
STARTUP_STARTING = "starting"
#: Up, on our version, answering /health.
STARTUP_RUNNING = "running"
#: A start has failed; `last_error` says why. Attempts go on after BACKOFF_SEC
#: and do NOT take the phase back to `starting`: the shop is told about the
#: malfunction until the till is really up (`running`), until it is activated
#: again, or until the mode is switched off (`idle`).
STARTUP_FAILED = "failed"


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


#: PET-1057 — the service answers only once its sync is over, and a long
#: queue takes longer than anyone wants to hold a browser request open.
SYNC_NOW_TIMEOUT = 25.0


def _timed_out(e: BaseException) -> bool:
    """Whether this is «it is taking too long», not «it went wrong»."""
    return isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)


def _post_sync(port: int, timeout: float = SYNC_NOW_TIMEOUT) -> Optional[dict]:
    """
    PET-1057 — ask the offline service to sync NOW (POST /api/sync).

    The answer when it gave one, None when it could not be asked at all.
    Running out of time is NOT «it could not be asked»: the sync carries on
    without us, so that answers «running» and the card looks again in a
    moment. Saying it failed would send a shop chasing a sync that is fine.

    No Origin and no Sec-Fetch-Site are sent, which is how the service tells
    its own callers from a web page.
    """
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/sync", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — refused, timeout, 4xx, not JSON
        if _timed_out(e):
            log.info("offline sync now is still running after %.0fs; left to finish on its own", timeout)
            return {"ran": "running", "failed": False}
        log.warning("offline sync now failed: %s", e)
        return None
    return body if isinstance(body, dict) else None


#: Asking the tax service (a grant, a registration form) goes over the
#: internet from the offline service, and the cabinet can be slow.
FISCAL_CALL_TIMEOUT = 60.0


def _call_fiscal(port: int, method: str, path: str, body: Optional[bytes], timeout: float = FISCAL_CALL_TIMEOUT) -> tuple[int, bytes]:
    """
    One call to the offline service's register routes (/api/offline-fiscal…)
    on the online app's behalf: the service lets only its own page and callers
    without an Origin in, and the online page is neither.

    The status and the body as the service answered them, 4xx included; a
    service that cannot be reached raises.
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/offline-fiscal{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@dataclass
class OfflineServiceState:
    """What the dashboard and the menu (PET-972) show."""

    running: bool = False
    pid: Optional[int] = None
    version: Optional[str] = None
    restarts: int = 0
    last_error: Optional[str] = None
    started_at: Optional[float] = None
    #: One of STARTUP_IDLE / STARTUP_STARTING / STARTUP_RUNNING /
    #: STARTUP_FAILED: what the online app shows while the till is not yet
    #: there — a start under way or a malfunction.
    startup: str = STARTUP_IDLE
    extra: dict = field(default_factory=dict)


class OfflineService:
    """Starts the offline service, keeps it on our version, restarts it."""

    def __init__(
        self,
        load_config: Callable[[], Optional[dict]],
        *,
        argv: Optional[list[str]] = None,
        expected_version: Optional[str] = None,
        port: Optional[int] = None,
        log_path: Optional[Path] = None,
        product: str = DEFAULT_PRODUCT,
        fetch_health: Callable[[int], Optional[dict]] = _fetch_health,
        post_sync: Callable[[int], Optional[dict]] = _post_sync,
        call_fiscal: Callable[[int, str, str, Optional[bytes]], tuple[int, bytes]] = _call_fiscal,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._load_config = load_config
        # FC-798 — which product's till this supervises: its service name,
        # port, runtime and data folder. Petshandler's are the defaults.
        spec = PRODUCTS[product]
        self.product = product
        self._service_name = spec.service_name
        self._fiscal = spec.fiscal
        # The other products' lines carry their name, so two tills on one
        # computer are told apart in the log; Petshandler's stay as they were.
        self._log = log if product == DEFAULT_PRODUCT else log.getChild(product)
        root = runtime_dir(product)
        self._argv = argv or [str(node_path(root)), str(service_script(root))]
        self._expected = expected_version if expected_version is not None else shipped_version(root)
        self._port = port if port is not None else spec.port
        self._log_path = log_path or (APP_DIR / spec.data_folder / "service.log")
        self._fetch = fetch_health
        self._post_sync = post_sync
        self._call_fiscal = call_fiscal
        self._sleep = sleep
        self._proc: Optional[asyncio.subprocess.Process] = None
        # PET-972 — the config the running service was started with: a new
        # activation or a switch-off restarts it.
        self._cfg_key: Optional[str] = None
        self._stopped_on_purpose = False
        self._pump: Optional[asyncio.Task] = None
        self._stopping = False
        self._failures = 0
        #: Raised by a fresh activation so a wait between attempts ends there
        #: and then, instead of leaving the person who has just switched the
        #: mode on in front of a till that has not even begun to start.
        self._wake = asyncio.Event()
        #: Counts the changes to what is activated here that the supervisor
        #: has been told about — a new activation, a changed till number, a
        #: switch-off — and `_attempt` is the one the round of the loop under
        #: way belongs to. An attempt may take half a minute, and a change
        #: arriving inside it replaces the configuration that attempt is
        #: working with: whatever it ends up saying — it failed, it is
        #: running — is about something nobody is waiting for any more, and
        #: must not be written over what the change says about itself.
        self._activation = 0
        self._attempt = 0
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
            # The activation this round of the loop works for. A new one
            # arriving in the middle of it supersedes this round, and what
            # this round then says about the till is no longer the answer.
            self._attempt = self._activation
            try:
                cfg = self._load_config()
            except SecretsUnreadable as e:
                # Not «not activated»: the secrets are there, just not readable
                # now. The till cannot be started with them, so this is a
                # failed start and not an endless one — and the wait ends on a
                # fresh activation, which may well be what makes them readable.
                self._fail(f"the saved offline secrets could not be read: {e}", once=True, started=False)
                await self._wait(IDLE_EVERY_SEC)
                continue
            if not cfg:
                if not told_idle:
                    self._log.info("offline service: the shop is not activated, nothing to start")
                    told_idle = True
                self.state.startup = STARTUP_IDLE
                await self._wait(IDLE_EVERY_SEC)
                continue
            told_idle = False
            if not self._runtime_ready():
                await self._wait(IDLE_EVERY_SEC)
                continue
            self._cfg_key = _config_key(cfg)
            # A switch-off belongs to the service it stopped, not to this one.
            self._stopped_on_purpose = False
            # Said before the attempt, so the whole of it — the port check, the
            # spawn, the wait for our version on /health — reads as a start
            # under way rather than as a till that is not running.
            if self.state.startup in (STARTUP_IDLE, STARTUP_STARTING, STARTUP_RUNNING):
                self.state.startup = STARTUP_STARTING
            else:
                # A start that has already failed keeps saying so until the
                # till is really up. The manager goes on retrying either way,
                # but the shop is not shown the malfunction, then a spinner,
                # then the malfunction again every few seconds: `failed` is
                # left only by a start that works, by activating again, or by
                # switching the mode off. So `starting` means «coming up and
                # nothing has failed since the activation», and the retry is
                # said here, or minutes of retrying would read as silence.
                self._log.info(
                    "offline service: starting again after a failure; still reported as a failed start"
                )
            try:
                started = await self._start(cfg)
                if started:
                    await self._watch()
            # Deliberately every exception, and only those: a cancellation is
            # not one of them (BaseException), so stopping still stops.
            except Exception as e:  # noqa: BLE001
                # A surprise anywhere in the attempt (a health check that
                # itself breaks, a log file that turns into a directory) used
                # to end the supervisor: the till then stayed dead and the
                # phase stayed `starting` for good. Said as a failed start
                # instead, and the next attempt follows as after any failure.
                self._fail(f"unexpected error while starting or watching: {e}", level=logging.ERROR)
            if self._stopping:
                break
            await self._stop_process()
            await self._wait(self._next_wait())

    async def stop(self) -> None:
        """Stop supervising and stop the service (manager shutdown)."""
        self._stopping = True
        await self._stop_process()

    async def stop_service(self) -> None:
        """Stop the running service now (switching offline mode off); the loop
        goes on and starts nothing until a shop is activated again."""
        self._stopped_on_purpose = True
        # Switching off is a change of what is activated here, so it counts as
        # one: an attempt already in flight belongs to what was switched off,
        # and «the port is taken» or «could not start» from it would be shown
        # as a malfunction to a shop that has just turned the mode off.
        self._activation += 1
        # Switched off on purpose: there is nothing starting and nothing broken.
        self.state.startup = STARTUP_IDLE
        await self._stop_process()

    def activation_changed(self) -> None:
        """A shop has just been activated here, or its till number changed.

        The supervisor reads the activation at the top of its loop, which may
        be a wait away, so the till takes a moment to come up. Said here, that
        moment reads as a start under way: the waits begin again from the
        shortest, the wait running right now ends, and the reason the PREVIOUS
        activation failed is dropped — kept, it would be shown as the reason
        this one failed.

        Nothing is said when the till is already serving this very activation
        — a second click on the same request, or a renewal that produced the
        same access. There is no start ahead of it, so calling it a start
        would leave a working till reported as starting for good.
        """
        if self.state.running and self._is_the_running_activation():
            self._log.info(
                "offline service: activated again with what the running till already serves; nothing to start"
            )
            return
        self._activation += 1
        self._failures = 0
        self.state.last_error = None
        self.state.startup = STARTUP_STARTING
        self._wake.set()

    def _is_the_running_activation(self) -> bool:
        """Whether what is activated now is exactly what the running service
        was started with. Anything else — a different activation, secrets
        that cannot be read just now — counts as not it, so a start is said
        to be under way rather than silently skipped."""
        try:
            key = _config_key(self._load_config())
        except SecretsUnreadable as e:
            self._log.warning("offline service: the activation could not be read, taken as a new one: %s", e)
            return False
        return key is not None and key == self._cfg_key

    async def _nap(self, seconds: float) -> None:
        """Sleep, ending early when a fresh activation is raised. The flag is
        left standing: only `_wait` consumes it, so a wakeup seen here is
        still there for the loop that acts on it."""
        if self._wake.is_set():
            return
        napping = asyncio.ensure_future(self._sleep(seconds))
        waking = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({napping, waking}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Whichever lost is dropped here: left running, the sleep would
            # fire into the next wait and the waiter would pile up one task
            # per attempt for as long as the manager lives.
            for task in (napping, waking):
                if not task.done():
                    task.cancel()

    async def _wait(self, seconds: float) -> None:
        """Wait between attempts, or until a fresh activation cuts it short."""
        await self._nap(seconds)
        # Consumed here and nowhere else, and only once a wait has seen it.
        # Cleared on the way in, as it was, it would be thrown away whenever
        # it was raised while the supervisor was busy starting or watching —
        # and the till would then sit out the whole wait it was meant to cut
        # short. The loop re-reads the activation the moment this returns.
        self._wake.clear()

    @property
    def port(self) -> int:
        """Where the offline till is served on this machine (PET-1055)."""
        return self._port

    async def refresh(self) -> Optional[dict]:
        """Ask /health now; the answer, when it is our service, or None."""
        health = await asyncio.to_thread(self._fetch, self._port)
        if not self._ours(health):
            return None
        self._remember(health)
        return health

    async def fiscal(self, method: str, path: str, body: Optional[bytes]) -> tuple[int, bytes]:
        """The online app's call to the register routes, passed through as is."""
        return await asyncio.to_thread(self._call_fiscal, self._port, method, path, body)

    async def sync_now(self) -> Optional[dict]:
        """
        PET-1057 — run the offline service's sync now, instead of waiting for
        its own schedule. The answer it gave, or None when it could not be
        asked. The state is refreshed afterwards so the card shows the new
        «as of» and queue length without a second round trip.
        """
        if not self.state.running:
            return None
        ran = await asyncio.to_thread(self._post_sync, self._port)
        if ran is None:
            return None
        await self.refresh()
        return ran

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
            if other.get("name") == self._service_name and await self._stop_stray(other):
                pass
            else:
                # A different program holds the port: do not start a second
                # service into it.
                self._fail(
                    f"port {self._port} is taken by {other.get('name')} {other.get('version')}"
                    f" (want {self._service_name} {self._expected})",
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
            if self._stopping or self._stopped_on_purpose or self._proc is not proc:
                return False
            if self._attempt != self._activation:
                # Activated again while this attempt was still waiting for an
                # answer. It is starting the service with the configuration of
                # the activation before, which nobody is waiting for: dropped
                # here instead of half a minute later, so the loop can start
                # the till for what is activated now.
                self._log.info("offline service: the activation changed while starting; this attempt is dropped")
                return False
            if proc.returncode is not None:
                self._fail(f"exited on start with code {proc.returncode}")
                return False
            health = await asyncio.to_thread(self._fetch, self._port)
            if self._stopping or self._proc is not proc:
                return False
            if self._ours(health):
                if self._attempt != self._activation:
                    # It came up, but for the activation before this one.
                    # Called running, it would hide the start the new
                    # activation is waiting for.
                    self._log.info(
                        "offline service: came up for an activation since replaced; starting again for the new one"
                    )
                    return False
                self.state = OfflineServiceState(
                    running=True,
                    pid=proc.pid,
                    version=health.get("version"),
                    restarts=self.state.restarts,
                    # Why it last had to be restarted stays visible.
                    last_error=self.state.last_error,
                    started_at=time.monotonic(),
                    startup=STARTUP_RUNNING,
                )
                self._remember(health)
                self._log.info(
                    "offline service %s started (pid %s, shop %s)",
                    self._expected, proc.pid, cfg.get("appid"),
                )
                return True
            if health is not None:
                self._fail(
                    f"answers as {health.get('name')} {health.get('version')},"
                    f" want {self._service_name} {self._expected}",
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
        if not _orphaned(pid):
            # Another manager (an overlapping update, a second session) may be
            # supervising it right now: killing it would stop a working till.
            self._log.warning(
                "offline service: %s (pid %s) on the port still has its manager; left alone",
                self._service_name, pid,
            )
            return False
        self._log.warning(
            "offline service: a stray %s %s (pid %s) holds the port, stopping it",
            self._service_name, health.get("version"), pid,
        )
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError) as e:
            self._log.warning("offline service: could not stop pid %s: %s", pid, e)
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
                # Reading never stops while the child runs: a child whose
                # pipe is not drained blocks on its next write.
                if out is None:
                    continue
                try:
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
                    self._log.warning("offline service: log write failed, output dropped from now on: %s", e)
                    try:
                        out.close()
                    except OSError:
                        pass
                    out = None
        finally:
            if out is not None:
                out.close()

    async def _watch(self) -> None:
        misses = 0
        while not self._stopping:
            # Ends early on a fresh activation, so renewing the access on a
            # running till replaces it now rather than at the next check.
            await self._nap(HEALTH_EVERY_SEC)
            if self._stopping:
                return
            proc = self._proc
            if self._stopped_on_purpose:
                # Switched off from the dashboard or Petshandler: not a failure.
                self._stopped_on_purpose = False
                return
            if self._attempt != self._activation:
                # The phase and the waits are the new activation's business
                # (activation_changed), so nothing is said about them here.
                self._log.info("offline service: activated again, replacing the running service")
                return
            if proc is None or proc.returncode is not None:
                self._fail(f"exited with code {proc.returncode if proc else '?'}")
                return
            try:
                now_key = _config_key(self._load_config())
            except SecretsUnreadable as e:
                # A read that fails now says nothing about the activation:
                # the running service keeps going.
                self._log.warning("offline service: secrets unreadable, keeping the running service: %s", e)
                now_key = self._cfg_key
            if now_key != self._cfg_key:
                self._log.info("offline service: the activation changed, restarting")
                self._failures = 0
                # A till number changed, or access was renewed: the service is
                # about to be replaced, not failing.
                self.state.startup = STARTUP_STARTING
                return
            health = await asyncio.to_thread(self._fetch, self._port)
            if self._ours(health):
                self._remember(health)
                misses = 0
                if self._what_was_seen_still_counts():
                    # Our own child, up and answering on our version: this is
                    # the one check that sees that, so nothing may leave a
                    # serving till reported as starting or broken.
                    self.state.startup = STARTUP_RUNNING
                started = self.state.started_at or time.monotonic()
                if self._failures and time.monotonic() - started >= STABLE_AFTER_SEC:
                    self._log.info("offline service stable again, restart waits reset")
                    self._failures = 0
                continue
            misses += 1
            if misses >= HEALTH_MISSES:
                self._fail(f"did not answer /health {misses} times in a row")
                return

    def _what_was_seen_still_counts(self) -> bool:
        """Whether a health check that has just come back is still the answer
        about this till.

        The check takes a moment, and in that moment the till may have been
        activated again or switched off — both of which say what the phase is
        (`starting`, `idle`) about a service that is on its way out but still
        answers. All three have to hold: the service is ours and held by this
        round of the loop, that round is still the current activation, and
        nothing has asked for it to stop. Checking only before the call left
        a renewal invisible to the app for as long as stopping the old service
        took, which is the whole of what this phase exists to show.
        """
        return (
            self.state.running
            and self._attempt == self._activation
            and not self._stopped_on_purpose
        )

    def _remember(self, health: dict) -> None:
        """What the dashboard shows and what the update guard asks."""
        self.state.extra = {
            "queued": health.get("queued"),
            "dataAsOf": health.get("dataAsOf"),
            "needsPairing": bool(health.get("needsPairing")),
            # PET-1122 — the till's own shift is open: Petshandler reminds the shop to close it.
            "shiftOpen": health.get("shiftOpen") is True,
            # Fiscal documents made offline not at the tax service yet: Petshandler's
            # «back online» dialog waits for them. Only a whole number counts.
            "fiscalPending": health.get("fiscalPending")
            if isinstance(health.get("fiscalPending"), int) and not isinstance(health.get("fiscalPending"), bool)
            else None,
            "appid": health.get("appid"),
        }
        if not self._fiscal:
            # FC-798 — a till without registers of its own says how it sees the
            # server instead (online / no internet / server down) and whether
            # its queue stopped until the service is updated.
            self.state.extra["connection"] = health.get("connection")
            self.state.extra["needsUpdate"] = health.get("needsUpdate") is True
            del self.state.extra["fiscalPending"]

    def _ours(self, health: Optional[dict]) -> bool:
        return (
            health is not None
            and health.get("name") == self._service_name
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
                # The pipe closes with the process; the copy then ends by
                # itself — but never wait on it without a limit.
                try:
                    await asyncio.wait_for(asyncio.gather(pump, return_exceptions=True), timeout=3)
                except asyncio.TimeoutError:
                    pump.cancel()
                except asyncio.CancelledError:
                    # Cancelled by whoever stops us: tidy up, then let the
                    # cancellation go on — swallowing it would hide it.
                    pump.cancel()
                    raise

    async def _end(self, proc: asyncio.subprocess.Process) -> None:
        # Closing stdin is the polite stop: the service shuts its server and
        # its database. Then SIGTERM, then SIGKILL.
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except asyncio.CancelledError:
            # Cancelled while waiting politely (shutdown): the child must not
            # outlive us, so it is killed before the cancellation goes on.
            _hard_kill(proc)
            raise
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
                self._log.error("offline service pid %s could not be killed: %s", proc.pid, e)
        self._log.warning("offline service did not stop on its own and was terminated")

    def _next_wait(self) -> float:
        wait = BACKOFF_SEC[min(self._failures, len(BACKOFF_SEC)) - 1] if self._failures else BACKOFF_SEC[0]
        self._log.info("offline service: next start in %.0fs (failure %d)", wait, self._failures)
        return wait

    def _fail(
        self, why: str, *, level: int = logging.WARNING, once: bool = False, started: bool = True,
    ) -> None:
        if self._attempt != self._activation:
            # An attempt that a newer activation has replaced, or that the
            # mode was switched off under. It has every right to fail — it was
            # working with a configuration that is gone — but its verdict is
            # not the answer any more, and written here it would show a
            # malfunction to someone who has just switched the mode on and is
            # waiting for a start, or just switched it off altogether.
            self._log.log(
                level,
                "offline service: %s — from an activation since replaced or switched off,"
                " left out of the state",
                why,
            )
            return
        if once and self.state.last_error == why:
            return
        self._failures += 1
        if started:
            # Shown as «restarts»: only a service that was really started.
            self.state.restarts += 1
        self.state.last_error = why
        # This is where the phase is set to «broken», for the endings that do
        # come through here: a missing runtime, secrets that cannot be read, a
        # port held by another program, an exit, silence on /health, a
        # surprise inside an attempt. It stays said until the till is really
        # up, or the activation changes, or the mode goes off — the retries in
        # between leave it alone. Other endings are not failures and are said
        # where they happen — a switch-off and a shop that is not activated say `idle`,
        # a start that worked says `running` — so a branch that gives up on
        # an attempt has to say what the phase is itself.
        self.state.startup = STARTUP_FAILED
        self._log.log(level, "offline service: %s", why)

    def _open_log(self, rotate: bool = False):
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            if rotate or (self._log_path.exists() and self._log_path.stat().st_size > LOG_MAX_BYTES):
                self._log_path.replace(self._log_path.with_suffix(".log.1"))
            return open(self._log_path, "ab")
        except OSError as e:
            # The output is then dropped (read and discarded), never mixed
            # into the manager's own log.
            self._log.warning("offline service: no log file (%s)", e)
            return None


def _config_key(cfg: Optional[dict]) -> Optional[str]:
    return json.dumps(cfg, sort_keys=True) if cfg else None


def _hard_kill(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _orphaned(pid: int) -> bool:
    """A service whose manager is gone. A child's stdin ends with its manager
    and it stops by itself, so a stray is one that hung past that — and on
    macOS/Linux it has been re-parented to launchd/init (ppid 1). On Windows
    there is no cheap parent check, and a hard-killed manager's child exits
    with its pipe anyway: nothing is killed there."""
    if sys.platform == "win32":
        return False
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip() == "1"


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
