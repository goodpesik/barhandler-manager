# Platform-specific notes

The manager has to run identically on three very different platforms. Each one has accumulated quirks worth knowing before touching installer code.

## macOS

### launchctl lies about exit codes

On macOS Sonoma (Intel especially), both `launchctl bootstrap` and the deprecated `launchctl load` print `Load failed: 5: Input/output error` to stderr **while returning exit code 0**. Any naive `if ! launchctl ...; then fallback; fi` never trips — the fallback skips, the installer claims success, and nothing is actually running.

**Fix:** After every `launchctl` call, poll `/health` for ~5 seconds. If nothing answers, treat launchd as a no-op regardless of exit code and fall through to `nohup` direct-spawn. Code in `installers/install.sh` lines ~210–260.

### `load`/`unload` are deprecated

Use `launchctl bootstrap gui/$(id -u) $PLIST` and `launchctl bootout gui/$(id -u)/<label>` instead. The error message that launchctl prints when it fails actually recommends `bootstrap` — that's where we got the hint.

### `--force` install doesn't kill the old process

`launchctl bootstrap` is a no-op when the process is already running. The installer used to swap files on disk (VERSION shows new) but the old python kept running with the old code in memory. Dashboard health.version would show stale value forever.

**Fix in `installers/install.sh`:** Before bringing the new process up:

```bash
launchctl bootout "$LAUNCH_TARGET" 2>/dev/null || true
pkill -f "$INSTALL_DIR/main.py" 2>/dev/null || true
# uvicorn graceful shutdown takes 5s+; wait, then SIGKILL stragglers
for i in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f "$INSTALL_DIR/main.py" >/dev/null 2>&1 || break
    sleep 1
done
pkill -9 -f "$INSTALL_DIR/main.py" 2>/dev/null || true
sleep 1
```

And the smoke test compares `.venv/bin/python --version`'s response against the `VERSION` file on disk — only declares success when the running process is the new version, not just "something answered".

### launchd subprocess PATH is bare

`launchctl`-spawned processes inherit `/usr/bin:/bin:/usr/sbin:/sbin` only. Homebrew (`/opt/homebrew/bin` on Apple Silicon, `/usr/local/bin` on Intel) is NOT on it. Dashboard's `POST /system/update` runs through this bare-PATH subprocess → `command -v brew` returns nothing → installer dies with "Homebrew not installed".

**Fix — three layers of defense:**
1. `src/routes/system.py` `trigger_update()` sets PATH explicitly in Popen's `env` arg.
2. Generated `update.sh` exports PATH at top before exec'ing installer.
3. `install.sh` exports PATH at the top AND has `find_brew()` that probes absolute paths (`/opt/homebrew/bin/brew`, `/usr/local/bin/brew`, `~/homebrew/bin/brew`) when `command -v brew` doesn't resolve.

### Python version resolution

`python3 -m venv` resolves to whatever `python3` PATH gives — usually Apple Command Line Tools' Python 3.8, which is too old for pydantic v2's PEP-585 type hints (`list[X]`). `brew install python@3.11` succeeds but puts the binary at `/opt/homebrew/opt/python@3.11/bin/python3.11`, not always linked on PATH as plain `python3`.

**Fix:** `find_python()` helper in `install.sh` probes versioned names (python3.13/3.12/3.11) and Homebrew @-version prefix dirs explicitly. Venv check runs `.venv/bin/python --version` and **recreates** if it's below 3.11 (otherwise an upgrade-then-reinstall leaves a venv pointing at a stale interpreter).

### libusb is a separate brew package

Linux installers pull `libusb-1.0-0` via apt. macOS branch never did the equivalent. Without it pyusb's `usb.core.find()` fails fast with `NoBackendError`, `discover_usb()` silently returns no printers. Fixed: `brew install libusb` after Python is sorted (idempotent — no-op if already present).

### Mixed content / HTTPS-to-HTTP

PWA on `https://*.barhandler.com` fetching `http://localhost:9999` — Desktop Chrome lets this through (localhost = secure context), but Android Chrome and some Safari versions don't. Symptom is identical to generic CORS failure. Diagnostic: open `http://localhost:9999/health` directly in the browser — if THAT works, mixed content isn't the issue. Real culprit is usually Chrome 117+ Private Network Access (see [troubleshooting.md](./troubleshooting.md)).

## Linux / Raspberry Pi

### Package install

apt: `python3 python3-venv python3-pip libusb-1.0-0`
dnf: `python3 python3-virtualenv python3-pip libusb1`
pacman: `python python-pip libusb`

### udev rules for USB printer access

Without these, USB printer ops need `sudo`. Installer drops `/etc/udev/rules.d/99-barhandler-manager.rules`:

```
SUBSYSTEM=="usb", ATTRS{bInterfaceClass}=="07", MODE="0660", GROUP="plugdev"
```

Plus `usermod -aG plugdev $USER`. Operator needs to log out + back in (or reboot) for the group change to take effect.

### systemd service

`/etc/systemd/system/barhandler-manager.service` — standard `Type=simple`, `Restart=on-failure`. `WorkingDirectory=$INSTALL_DIR`, `User=$USER`. `systemctl enable --now`.

## Termux / Android

### `_is_termux()` detection

Canonical marker:
```python
os.environ.get("PREFIX", "").startswith("/data/data/com.termux/")
```
Termux sets `PREFIX` in every shell; no non-Termux process would set that value. More reliable than `platform.system()` which returns `"Linux"` on Android the same as on Ubuntu.

### USB / Bluetooth — not supported

**Decision:** Android = network printers only.

- **USB:** pyusb can't reach Android USB stack from Termux without per-device `termux-usb` permissions, and even then the workflow is one-at-a-time + interactive prompts. `NoBackendError` or `USBError` on every `find_all=True` call. `discover_usb()` early-returns `[]` when `_is_termux()` to avoid noisy logs.
- **Bluetooth:** Termux:API has no `termux-bluetooth-*` commands. Android's `BluetoothAdapter` is a Java framework class needing an Activity context with BLUETOOTH permission. No path from pure Python in Termux without a companion APK. `bluetoothctl` (BlueZ) isn't packaged in Termux. `discover_bluetooth()` early-returns `[]`.

Operator-facing advisory in `_discovery_warnings()` explains this: "На Android підтримуються лише мережеві принтери. Підключіть принтер до Wi-Fi і скористайтесь пошуком."

### pkg install order

`install-android.sh` sequence MUST be:
1. `pkg update -y` — refresh index
2. `pkg upgrade -y` — pull newer versions, syncs libcurl/libngtcp2 ABI
3. `pkg install -y python rust binutils libusb termux-api termux-services curl wget tar rsync libjpeg-turbo libpng zlib freetype`

**Skipping step 2:** fresh Termux ends up with libcurl built against older libngtcp2 → `pkg install` pulls newer libngtcp2 → curl dies on next call with `CANNOT LINK EXECUTABLE ".../bin/curl": cannot locate symbol "ngtcp2_crypto_get_path_challenge_data2_cb"`.

**Skipping `libjpeg-turbo libpng zlib freetype`:** pip install fails compiling Pillow from source with `RequiredDependencyException: jpeg`. Pillow has **no prebuilt wheel for android_arm64** — pip always compiles from source. python-escpos transitively requires Pillow.

### Python version bumps under venv

Termux periodically bumps default `python` (3.11 → 3.13 mid-2026 for example). A venv created against the old python keeps pointing at a replaced binary; subsequent `pip install` misses precompiled wheels and falls back to rust compilation.

**Fix in `install-android.sh`:** before pip install, compare system python version to venv python version. If different, `rm -rf .venv && python -m venv $INSTALL_DIR/.venv`. Plus export `ANDROID_API_LEVEL=24` so maturin (build backend for pydantic-core) doesn't bail with "Failed to determine Android API level".

### termux-services runit

- Service def at `$PREFIX/var/service/<service-name>/run` (executable shell)
- `sv-enable <name>` + `sv up <name>` to start
- `sv down <name>` to stop
- `runsvdir` (the scanner) starts on first interactive shell login. On fresh install the named pipes inside `supervise/` don't exist yet, so `sv up` prints `fail: <svc>: unable to change to service directory: file does not exist` — benign noise. **Always pipe sv output to /dev/null.**
- **Direct nohup fallback required:** after `sv up`, poll `/health` for ~1s. If no response, `cd $INSTALL_DIR && nohup python main.py > bhm.boot.log 2>&1 & disown`. Runit takes over on next reboot.

### Bare PATH in service-context subprocess

Same trap as launchd on macOS. Generated `update.sh` exports `PATH=/data/data/com.termux/files/usr/bin:...` before exec'ing the installer.

### Chrome 117+ Private Network Access (PNA)

The HARDEST debug of the entire project — full writeup in [troubleshooting.md → "PWA can't reach localhost manager"](./troubleshooting.md#pwa-cant-reach-localhost-manager).

TL;DR: Chrome 117+ blocks fetches from public origin (https://...barhandler.com) to private network (anything in 127/8) without explicit `Access-Control-Allow-Private-Network: true` on preflight. Starlette's CORSMiddleware refuses these preflights by default with no opt-in flag — we short-circuit them ourselves in our `cors_and_pna` middleware before they reach Starlette.

## Windows

`installers/install.ps1` exists but is field-untested. Treat with caution; expect to fix things on first real-world deploy.
