#!/usr/bin/env bash
# PET-971 — lays out a product's offline runtime for the installers:
#
#   <out>/device-handler-offline[.exe]   the official Node binary, renamed so
#                                        install/uninstall scripts can stop it
#   <out>/service/main.js                the offline service bundle
#   <out>/service/version.txt            its version (the manager checks /health for it)
#   <out>/app/                           the offline build of the app
#
# Usage: build_offline_runtime.sh <platform> <out> <service-checkout> <app-dist>
#   platform: win-x64 | darwin-arm64 | darwin-x64 | linux-x64 | linux-arm64
#   service-checkout: a built petshandler-offline (dist/main.js, package.json)
#   app-dist: petshandler-app's dist-offline
# Env: NODE_VERSION (required, e.g. v22.11.0 — pinned by the workflow),
#      NODE_DIST_BASE (default https://nodejs.org/dist; file:// in tests).
#
# The Node archive is checked against the SHASUMS256.txt of the same release
# before anything is taken from it.
set -euo pipefail

PLATFORM="${1:?platform}"
OUT="${2:?out dir}"
SERVICE="${3:?service checkout}"
APP="${4:?app dist}"
: "${NODE_VERSION:?NODE_VERSION is required (e.g. v22.11.0)}"
BASE="${NODE_DIST_BASE:-https://nodejs.org/dist}"

case "$PLATFORM" in
  win-x64) ARCHIVE="node-${NODE_VERSION}-win-x64.zip"; BIN="node-${NODE_VERSION}-win-x64/node.exe"; NAME="device-handler-offline.exe" ;;
  darwin-arm64|darwin-x64|linux-x64|linux-arm64)
    ARCHIVE="node-${NODE_VERSION}-${PLATFORM}.tar.gz"; BIN="node-${NODE_VERSION}-${PLATFORM}/bin/node"; NAME="device-handler-offline" ;;
  *) echo "::error::unknown platform $PLATFORM"; exit 1 ;;
esac

[ -f "$SERVICE/dist/main.js" ] || { echo "::error::no $SERVICE/dist/main.js — build the service first"; exit 1; }
[ -f "$APP/index.html" ] || { echo "::error::no $APP/index.html — build the offline app first"; exit 1; }

WORK="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/offline-runtime-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

echo "==> Node $NODE_VERSION for $PLATFORM"
curl -fsSL "$BASE/$NODE_VERSION/SHASUMS256.txt" -o "$WORK/SHASUMS256.txt"
curl -fsSL "$BASE/$NODE_VERSION/$ARCHIVE" -o "$WORK/$ARCHIVE"
EXPECTED="$(awk -v f="$ARCHIVE" '$2 == f { print $1 }' "$WORK/SHASUMS256.txt")"
[ -n "$EXPECTED" ] || { echo "::error::$ARCHIVE is not in SHASUMS256.txt"; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL="$(sha256sum "$WORK/$ARCHIVE" | awk '{print $1}')"
else
  ACTUAL="$(shasum -a 256 "$WORK/$ARCHIVE" | awk '{print $1}')"
fi
[ "$EXPECTED" = "$ACTUAL" ] || { echo "::error::checksum mismatch for $ARCHIVE"; exit 1; }

case "$ARCHIVE" in
  *.zip) unzip -q "$WORK/$ARCHIVE" "$BIN" -d "$WORK" ;;
  *) tar -xzf "$WORK/$ARCHIVE" -C "$WORK" "$BIN" ;;
esac

mkdir -p "$OUT/service" "$OUT/app"
cp "$WORK/$BIN" "$OUT/$NAME"
chmod 755 "$OUT/$NAME"
cp "$SERVICE/dist/main.js" "$OUT/service/main.js"
VERSION="$(sed -n 's/^[[:space:]]*"version":[[:space:]]*"\([^"]*\)".*/\1/p' "$SERVICE/package.json" | head -n1)"
[ -n "$VERSION" ] || { echo "::error::no version in $SERVICE/package.json"; exit 1; }
printf '%s\n' "$VERSION" > "$OUT/service/version.txt"
cp -R "$APP/." "$OUT/app/"
echo "==> Offline runtime $VERSION laid out in $OUT"
