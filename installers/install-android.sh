#!/data/data/com.termux/files/usr/bin/env bash
# barhandler-manager installer for Android / Termux.
#
# Termux is a userland Linux on Android — `pkg` is its apt-equivalent.
# USB host access goes through `termux-usb` (requires the Termux:API
# add-on app from F-Droid). Service registration uses `termux-services`
# (sv-style supervision) instead of systemd.

set -euo pipefail

REPO="goodpesik/barhandler-manager"
INSTALL_DIR="${HOME}/.barhandler-manager"
SERVICE_NAME="barhandler-manager"
FORCE=0

for arg in "${@:-}"; do
    case "$arg" in
        -f|--force|--upgrade) FORCE=1 ;;
    esac
done

say() { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m⚠\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

is_running() { curl -fsS --max-time 1 http://localhost:9999/health >/dev/null 2>&1; }
is_installed() { [ -x "$INSTALL_DIR/.venv/bin/python" ] && [ -f "$INSTALL_DIR/main.py" ]; }

# --- sanity check ----------------------------------------------------
if [ -z "${PREFIX:-}" ] || [ "$PREFIX" != "/data/data/com.termux/files/usr" ]; then
    die "this installer is Termux-only — use install.sh on regular Linux"
fi
say "platform: Android (Termux)"

# --- short-circuit if already installed ------------------------------
if is_installed && [ $FORCE -eq 0 ]; then
    if is_running; then
        say "barhandler-manager is already installed and running at http://localhost:9999"
        say "    → re-run with --force to upgrade to the latest release"
        exit 0
    else
        say "installed but not running — starting it"
        sv up "$SERVICE_NAME" || true
        exit 0
    fi
fi

# --- packages --------------------------------------------------------
say "updating Termux packages"
pkg update -y
pkg install -y python rust binutils libusb termux-api termux-services curl tar rsync

# --- download release ------------------------------------------------
mkdir -p "$INSTALL_DIR"
TARBALL_URL="https://github.com/${REPO}/archive/refs/heads/production.tar.gz"
TMP="$(mktemp -d)"
curl -fsSL "$TARBALL_URL" -o "$TMP/src.tar.gz" || die "couldn't fetch release tarball"
tar -xzf "$TMP/src.tar.gz" -C "$TMP"
SRC_ROOT="$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -n1)"

# Preserve user config.
for keep in config.yaml printers.json terminals.json; do
    [ -f "$INSTALL_DIR/$keep" ] && cp -a "$INSTALL_DIR/$keep" "$TMP/$keep.bak"
done

rsync -a --exclude='.venv' --exclude='config.yaml' --exclude='printers.json' --exclude='terminals.json' "$SRC_ROOT/" "$INSTALL_DIR/"

for keep in config.yaml printers.json terminals.json; do
    [ -f "$TMP/$keep.bak" ] && cp -a "$TMP/$keep.bak" "$INSTALL_DIR/$keep"
done

rm -rf "$TMP"

# --- venv + deps ------------------------------------------------------
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    say "creating virtualenv"
    python -m venv "$INSTALL_DIR/.venv"
fi

say "installing Python dependencies (this is the slow part on Android — Rust crates for cryptography compile)"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# --- termux-services unit --------------------------------------------
SV_DIR="${PREFIX}/var/service/${SERVICE_NAME}"
mkdir -p "$SV_DIR/log"
cat > "$SV_DIR/run" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
exec 2>&1
cd ${INSTALL_DIR}
exec ${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/main.py
EOF
chmod +x "$SV_DIR/run"

cat > "$SV_DIR/log/run" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
exec svlogd -tt ${INSTALL_DIR}/log
EOF
chmod +x "$SV_DIR/log/run"
mkdir -p "$INSTALL_DIR/log"

# Enable + start
sv-enable "$SERVICE_NAME" 2>/dev/null || true
sv up "$SERVICE_NAME" 2>/dev/null || true

# --- helper scripts --------------------------------------------------
cat > "$INSTALL_DIR/start.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/env bash
if curl -fsS --max-time 1 http://localhost:9999/health >/dev/null 2>&1; then
    echo "✓ already running at http://localhost:9999"
    exit 0
fi
echo "▸ starting barhandler-manager"
sv up $SERVICE_NAME
sleep 2
curl -fsS --max-time 2 http://localhost:9999/health >/dev/null 2>&1 \
    && echo "✓ running" \
    || { echo "⚠ check $INSTALL_DIR/log/current"; exit 1; }
EOF
cat > "$INSTALL_DIR/stop.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/env bash
sv down $SERVICE_NAME && echo "▸ stopped"
EOF
cat > "$INSTALL_DIR/status.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/env bash
curl -fsS --max-time 1 http://localhost:9999/health 2>/dev/null \
    && echo && echo "✓ running" \
    || echo "✗ not reachable"
EOF
cat > "$INSTALL_DIR/update.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/env bash
# Re-run the Termux installer in upgrade mode.
exec curl -fsSL https://github.com/${REPO}/releases/latest/download/install-android.sh | bash -s -- --force
EOF

chmod +x "$INSTALL_DIR/start.sh" "$INSTALL_DIR/stop.sh" "$INSTALL_DIR/status.sh" "$INSTALL_DIR/update.sh"

# --- USB note --------------------------------------------------------
cat <<EOF

╭──────────────────────────────────────────────────────────────╮
│  Installed under: ${INSTALL_DIR}
│  Logs:            ${INSTALL_DIR}/log/current
│
│  USB hardware: Termux can talk to USB devices via the
│                'termux-usb' command (Termux:API app required).
│                When you plug a USB printer in for the first
│                time, Android pops up a permission dialog —
│                accept it and the manager will see the device.
│
│  Helpers (in install dir):
│   ${INSTALL_DIR}/start.sh
│   ${INSTALL_DIR}/stop.sh
│   ${INSTALL_DIR}/status.sh
│   ${INSTALL_DIR}/update.sh     ← fetches the latest release
│
│  Next steps:
│   1. Open your POS web app
│   2. Settings → Integrations → "Use device manager" → toggle ON
│   3. Click "Discover printers"
╰──────────────────────────────────────────────────────────────╯
EOF
