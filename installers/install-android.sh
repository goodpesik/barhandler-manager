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
# `pkg update` only refreshes the index; `pkg upgrade` actually pulls
# newer versions. Without the upgrade step, a freshly-flashed Termux
# install often ends up with libcurl built against an older libngtcp2
# than the one `pkg install` just pulled in, and the next curl call
# dies with:
#   CANNOT LINK EXECUTABLE ".../curl": cannot locate symbol
#   "ngtcp2_crypto_get_path_challenge_data2_cb" referenced by libcurl.so
# Upgrading first keeps the shared-library graph in sync.
say "updating Termux package index"
pkg update -y
say "upgrading existing Termux packages (keeps libcurl/libngtcp2 in sync)"
pkg upgrade -y
say "installing required Termux packages"
# Termux has no prebuilt Pillow wheel for android_arm64, so pip has to
# compile it from source. Pillow links against jpeg + png + zlib +
# freetype at build time — missing any of those gives:
#   RequiredDependencyException: jpeg
# (or png, zlib, freetype). We don't import Pillow ourselves, but
# python-escpos does (for image-based receipts / labels), so the install
# fails at pip step without these headers. Add them up front.
pkg install -y \
    python rust binutils libusb termux-api termux-services \
    curl wget tar rsync \
    libjpeg-turbo libpng zlib freetype

# Termux occasionally still ships a curl that can't link even after an
# upgrade if the user interrupted a previous install. Reinstall the
# pair atomically so they're guaranteed to share an ABI.
if ! curl -fsS --max-time 1 https://github.com >/dev/null 2>&1; then
    warn "curl can't link to libcurl — reinstalling libcurl + libngtcp2"
    pkg install -y --reinstall libcurl libngtcp2 curl || true
fi

# --- download release ------------------------------------------------
mkdir -p "$INSTALL_DIR"
TARBALL_URL="https://github.com/${REPO}/archive/refs/heads/production.tar.gz"
TMP="$(mktemp -d)"

# Try curl, then wget as fallback. Either covers Termux's libcurl-ABI
# breakage so the operator isn't stuck re-running pkg by hand.
fetch_tarball() {
    if curl -fsSL "$TARBALL_URL" -o "$TMP/src.tar.gz" 2>/dev/null; then
        return 0
    fi
    warn "curl failed — falling back to wget"
    if command -v wget >/dev/null && wget -q "$TARBALL_URL" -O "$TMP/src.tar.gz"; then
        return 0
    fi
    return 1
}
fetch_tarball || die "couldn't fetch release tarball (tried curl + wget)"
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

# Enable + start. `sv` talks to `runsv` over named pipes inside
# $SV_DIR/supervise — but those pipes only exist once `runsvdir` (the
# scanner) has noticed the new service directory. On a fresh install
# that hasn't happened yet, and `sv up` prints the noisy:
#   fail: barhandler-manager: unable to change to service directory: ...
# Redirect ALL output (some sv versions write to fd 1 not fd 2) and
# tolerate the failure — we have a direct-spawn fallback below.
sv-enable "$SERVICE_NAME" >/dev/null 2>&1 || true
sv up "$SERVICE_NAME" >/dev/null 2>&1 || true

# Fallback: if runsv hasn't picked up the service yet (common on a
# fresh install — runsvdir starts at next shell login), spawn the
# Python directly via nohup so the manager is running RIGHT NOW. The
# user gets working software immediately; runit takes over on reboot.
sleep 1
if ! curl -fsS --max-time 1 http://localhost:9999/health >/dev/null 2>&1; then
    say "service supervisor not ready — spawning manager directly"
    nohup "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/main.py" \
        > "$INSTALL_DIR/bhm.boot.log" 2>&1 &
    disown 2>/dev/null || true
    sleep 2
fi

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
│                   ${INSTALL_DIR}/bhm.log (rotated app log)
│
│  Dashboard:       http://localhost:9999
│                   (open in any browser on the same device —
│                   shows printer / terminal status, live logs,
│                   "Check for updates" button)
│
│  Health check:    curl http://localhost:9999/health
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
│   1. Open Dashboard at http://localhost:9999 to verify it's up
│   2. In your POS web app: Settings → Integrations →
│      "Use device manager" → toggle ON
│   3. Click "Discover printers" / "Discover POS terminals"
╰──────────────────────────────────────────────────────────────╯
EOF
