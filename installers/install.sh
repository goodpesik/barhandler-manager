#!/usr/bin/env bash
# barhandler-manager installer for macOS / Linux / Raspberry Pi.
#
# Idempotent — re-running upgrades to the latest release without
# touching config.yaml / printers.json. Detects the package manager
# (brew on macOS, apt elsewhere) only to install Python if it's
# missing; everything Python-side runs inside a private virtualenv
# under ~/.barhandler-manager/.venv so we never collide with the
# system Python.
#
# Behavior matrix:
#   not installed         → full install + start
#   installed + running   → no-op (use --force / -f to upgrade)
#   installed + stopped   → restart the service
#
# After a successful install we drop start.sh / stop.sh / status.sh
# into the install dir for manual control alongside the OS service.

set -euo pipefail

REPO="goodpesik/barhandler-manager"
INSTALL_DIR="${HOME}/.barhandler-manager"
SERVICE_NAME="barhandler-manager"
PY_MIN="3.11"
FORCE=0

for arg in "${@:-}"; do
    case "$arg" in
        -f|--force|--upgrade) FORCE=1 ;;
    esac
done

# --- pretty output ---------------------------------------------------
say() { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m⚠\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

is_running() {
    curl -fsS --max-time 1 http://localhost:9999/health >/dev/null 2>&1
}

is_installed() {
    [ -x "$INSTALL_DIR/.venv/bin/python" ] && [ -f "$INSTALL_DIR/main.py" ]
}

# --- short-circuit if already installed ------------------------------
if is_installed && [ $FORCE -eq 0 ]; then
    if is_running; then
        say "barhandler-manager is already installed and running at http://localhost:9999"
        say "    → re-run with --force to upgrade to the latest release"
        exit 0
    else
        say "installed but not running — starting it"
        "$INSTALL_DIR/start.sh"
        exit 0
    fi
fi

# --- detect OS -------------------------------------------------------
OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM=macos ;;
    Linux)
        if [ -f /etc/rpi-issue ] || grep -q Raspberry /proc/cpuinfo 2>/dev/null; then
            PLATFORM=raspberry
        else
            PLATFORM=linux
        fi
        ;;
    *) die "unsupported OS: $OS (use install.ps1 on Windows, install-android.sh in Termux)" ;;
esac
say "platform: $PLATFORM"

# --- ensure Python 3.11+ ---------------------------------------------
have_python() {
    command -v python3 >/dev/null && \
        python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
}

if ! have_python; then
    say "Python ${PY_MIN}+ not found — installing"
    case "$PLATFORM" in
        macos)
            if ! command -v brew >/dev/null; then
                die "Homebrew not installed. Get it from https://brew.sh and re-run."
            fi
            brew install python@3.11
            ;;
        raspberry|linux)
            if command -v apt >/dev/null; then
                sudo apt update
                sudo apt install -y python3 python3-venv python3-pip libusb-1.0-0
            elif command -v dnf >/dev/null; then
                sudo dnf install -y python3 python3-virtualenv python3-pip libusb1
            elif command -v pacman >/dev/null; then
                sudo pacman -Sy --noconfirm python python-pip libusb
            else
                die "no supported package manager (apt/dnf/pacman) found"
            fi
            ;;
    esac
fi
say "python: $(python3 --version)"

# --- udev rules (Linux only — give the user access to USB printers) --
if [ "$PLATFORM" != "macos" ]; then
    UDEV_RULE="/etc/udev/rules.d/99-barhandler-manager.rules"
    if [ ! -f "$UDEV_RULE" ]; then
        say "installing udev rules so printers are reachable without sudo"
        # Printer-class (07) gets group-read on plugdev — the default for
        # non-root users on Debian/Ubuntu/Raspberry Pi OS.
        echo 'SUBSYSTEM=="usb", ATTRS{bInterfaceClass}=="07", MODE="0660", GROUP="plugdev"' | \
            sudo tee "$UDEV_RULE" >/dev/null
        sudo udevadm control --reload-rules
        sudo udevadm trigger
        sudo usermod -aG plugdev "$USER" || warn "couldn't add $USER to plugdev — print may need sudo until you do"
    fi
fi

# --- download latest release tarball ---------------------------------
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

say "fetching latest release"
TARBALL_URL="https://github.com/${REPO}/archive/refs/heads/production.tar.gz"
TMP="$(mktemp -d)"
curl -fsSL "$TARBALL_URL" -o "$TMP/src.tar.gz" || die "couldn't fetch release tarball"
tar -xzf "$TMP/src.tar.gz" -C "$TMP"
SRC_ROOT="$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -n1)"

# Preserve user config across re-installs.
for keep in config.yaml printers.json terminals.json; do
    [ -f "$INSTALL_DIR/$keep" ] && cp -a "$INSTALL_DIR/$keep" "$TMP/$keep.bak"
done

# Copy fresh code over the install dir (everything except .venv / user data).
rsync -a --exclude='.venv' --exclude='config.yaml' --exclude='printers.json' --exclude='terminals.json' "$SRC_ROOT/" "$INSTALL_DIR/"

# Restore user config (only if upgrade; fresh install uses shipped config.yaml).
for keep in config.yaml printers.json terminals.json; do
    [ -f "$TMP/$keep.bak" ] && cp -a "$TMP/$keep.bak" "$INSTALL_DIR/$keep"
done

rm -rf "$TMP"

# --- venv + deps ------------------------------------------------------
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    say "creating virtualenv"
    python3 -m venv "$INSTALL_DIR/.venv"
fi
say "installing Python dependencies"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# --- service registration --------------------------------------------
case "$PLATFORM" in
    macos)
        PLIST="$HOME/Library/LaunchAgents/com.goodpesik.barhandler-manager.plist"
        mkdir -p "$(dirname "$PLIST")"
        cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.goodpesik.barhandler-manager</string>
    <key>ProgramArguments</key>
    <array>
        <string>${INSTALL_DIR}/.venv/bin/python</string>
        <string>${INSTALL_DIR}/main.py</string>
    </array>
    <key>WorkingDirectory</key><string>${INSTALL_DIR}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>${INSTALL_DIR}/bhm.boot.log</string>
    <key>StandardErrorPath</key><string>${INSTALL_DIR}/bhm.boot.log</string>
</dict>
</plist>
EOF
        # launchctl `load`/`unload` are deprecated since macOS Catalina
        # and on Sonoma they fail with `Load failed: 5: Input/output error`
        # on Intel boxes (observed in the field). Use the modern
        # bootstrap/bootout API targeting the user's gui domain — that's
        # what the launchctl EIO message itself recommends ("Try running
        # launchctl bootstrap as root for richer errors").
        LAUNCH_DOMAIN="gui/$(id -u)"
        LAUNCH_TARGET="$LAUNCH_DOMAIN/com.goodpesik.barhandler-manager"
        # bootout is a no-op if the service isn't loaded — swallow the
        # error so re-runs of install.sh work.
        launchctl bootout "$LAUNCH_TARGET" 2>/dev/null || true
        if ! launchctl bootstrap "$LAUNCH_DOMAIN" "$PLIST"; then
            warn "launchctl bootstrap failed — falling back to legacy load"
            launchctl load "$PLIST"
        fi
        say "launchd service installed and started"
        ;;
    raspberry|linux)
        UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
        sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Barhandler Manager (local hardware bridge)
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/main.py
Restart=on-failure
StandardOutput=append:${INSTALL_DIR}/bhm.boot.log
StandardError=append:${INSTALL_DIR}/bhm.boot.log

[Install]
WantedBy=multi-user.target
EOF
        sudo systemctl daemon-reload
        sudo systemctl enable --now "$SERVICE_NAME"
        say "systemd service installed and started"
        ;;
esac

# --- drop helper scripts so users don't need to remember launchctl ---
case "$PLATFORM" in
    macos)
        # Modern launchctl: bootstrap/bootout (not deprecated load/unload).
        # The $(id -u) is captured at script-generation time so start.sh
        # works for the operator regardless of which shell they run it from.
        SERVICE_CMD_START="launchctl bootstrap gui/$(id -u) $HOME/Library/LaunchAgents/com.goodpesik.barhandler-manager.plist"
        SERVICE_CMD_STOP="launchctl bootout gui/$(id -u)/com.goodpesik.barhandler-manager"
        ;;
    raspberry|linux)
        SERVICE_CMD_START="sudo systemctl start $SERVICE_NAME"
        SERVICE_CMD_STOP="sudo systemctl stop $SERVICE_NAME"
        ;;
esac

cat > "$INSTALL_DIR/start.sh" <<EOF
#!/usr/bin/env bash
# Start barhandler-manager (or report that it's already running).
if curl -fsS --max-time 1 http://localhost:9999/health >/dev/null 2>&1; then
    echo "✓ already running at http://localhost:9999"
    exit 0
fi
echo "▸ starting barhandler-manager"
if ! $SERVICE_CMD_START; then
    echo "⚠ service manager (launchctl/systemd) refused — falling back to direct spawn"
    # nohup keeps it alive after this shell closes; \`disown\` removes it
    # from the shell's job table so Ctrl+C here doesn't kill it.
    nohup "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/main.py" \\
        > "$INSTALL_DIR/bhm.boot.log" 2>&1 &
    disown 2>/dev/null || true
fi
sleep 2
if curl -fsS --max-time 2 http://localhost:9999/health >/dev/null 2>&1; then
    echo "✓ running at http://localhost:9999"
else
    echo "⚠ didn't answer within 2s — check $INSTALL_DIR/bhm.boot.log"
    exit 1
fi
EOF

cat > "$INSTALL_DIR/stop.sh" <<EOF
#!/usr/bin/env bash
echo "▸ stopping barhandler-manager"
# Whichever route start.sh took (service manager vs direct spawn) we
# want stop.sh to kill both — bootout is a no-op if not loaded, pkill
# is a no-op if no process matches.
$SERVICE_CMD_STOP 2>/dev/null || true
pkill -f "$INSTALL_DIR/main.py" 2>/dev/null || true
EOF

cat > "$INSTALL_DIR/status.sh" <<EOF
#!/usr/bin/env bash
if curl -fsS --max-time 1 http://localhost:9999/health 2>/dev/null; then
    echo
    echo "✓ running"
else
    echo "✗ not reachable on http://localhost:9999"
    echo "  logs: $INSTALL_DIR/bhm.log"
fi
EOF

# update.sh: fetches the upstream installer with --force so the
# operator doesn't have to remember the URL.
cat > "$INSTALL_DIR/update.sh" <<EOF
#!/usr/bin/env bash
# Pull the latest barhandler-manager release. Re-runs install.sh in
# upgrade mode (config.yaml / printers.json preserved, code + venv
# refreshed). Equivalent to:
#   curl -fsSL https://github.com/${REPO}/releases/latest/download/install.sh | bash -s -- --force
exec curl -fsSL https://github.com/${REPO}/releases/latest/download/install.sh | bash -s -- --force
EOF

chmod +x "$INSTALL_DIR/start.sh" "$INSTALL_DIR/stop.sh" "$INSTALL_DIR/status.sh" "$INSTALL_DIR/update.sh"

# --- smoke test -------------------------------------------------------
sleep 3
if is_running; then
    say "✓ manager is up at http://localhost:9999"
else
    warn "manager didn't answer /health within 3s — check ${INSTALL_DIR}/bhm.log"
fi

cat <<EOF

╭──────────────────────────────────────────────────────────────╮
│  Installed under: ${INSTALL_DIR}
│  Logs:            ${INSTALL_DIR}/bhm.log
│  Config:          ${INSTALL_DIR}/config.yaml
│
│  Helpers (in install dir):
│   ${INSTALL_DIR}/start.sh
│   ${INSTALL_DIR}/stop.sh
│   ${INSTALL_DIR}/status.sh
│   ${INSTALL_DIR}/update.sh     ← fetches the latest release
│
│  Dashboard:       http://localhost:9999/
│
│  Next steps:
│   1. Open http://localhost:9999/ to see the dashboard
│   2. Open your POS web app
│   3. Settings → Integrations → "Use device manager" → toggle ON
│   4. Click "Discover printers"
╰──────────────────────────────────────────────────────────────╯
EOF
