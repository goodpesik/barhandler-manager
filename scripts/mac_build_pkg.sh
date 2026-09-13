#!/usr/bin/env bash
#
# BH-150 — зібрати .pkg-інсталятор із уже підписаного «Device Handler.app».
#
#   scripts/mac_build_pkg.sh <шлях до .app> <шлях до .pkg на виході>
#
# Кличеться ПІСЛЯ scripts/mac_sign_and_package.sh: той підписує застосунок
# сертифікатом Developer ID Application, цей пакує його в інсталятор і
# підписує вже сертифікатом Developer ID **Installer** — це інший сертифікат,
# і підмінити один одним не можна (productbuild просто відмовиться).
#
# Обидва режими, як і в сусідньому скрипті, вибирає наявність секретів:
# є Developer ID Installer у keychain — підписуємо й нотаризуємо, немає —
# віддаємо непідписаний .pkg, щоб форк не ловив червоне CI.
#
# Нотаризація .pkg відбувається ТУТ, а не в сусідньому скрипті: усередині
# пакета лежить уже нотаризований застосунок, але Gatekeeper перевіряє саме
# той файл, який людина двічі клікнула.
set -euo pipefail

APP="${1:?перший аргумент — шлях до .app}"
PKG="${2:?другий аргумент — шлях до .pkg на виході}"
IDENTIFIER="com.goodpesik.barhandler-manager.app"
DIST="${MAC_PKG_DISTRIBUTION:-installers/mac-distribution.xml}"
RESOURCES="${MAC_PKG_RESOURCES:-installers/mac-resources}"
POSTINSTALL="${MAC_PKG_POSTINSTALL:-installers/mac-postinstall.sh}"
VERSION="$(tr -d '[:space:]' < VERSION 2>/dev/null || echo 0.0.0)"

[ -d "$APP" ] || { echo "::error::немає $APP"; exit 1; }
[ -f "$DIST" ] || { echo "::error::немає $DIST"; exit 1; }

WORKDIR=""
cleanup() { [ -n "$WORKDIR" ] && rm -rf "$WORKDIR" || true; }
trap cleanup EXIT

WORKDIR="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/bhm-pkg-XXXXXX")"
SCRIPTS="$WORKDIR/scripts"
ROOT="$WORKDIR/root/Applications"
mkdir -p "$SCRIPTS" "$ROOT"

# --- вміст пакета -----------------------------------------------------------
# Копіюємо ПІДПИСАНИЙ застосунок як є: перепакування ламає підпис, і далі
# нотаризація відкидає весь .pkg.
cp -R "$APP" "$ROOT/"
install -m 755 "$POSTINSTALL" "$SCRIPTS/postinstall"

# --- заборона переміщення -----------------------------------------------------
# Знайдено на живій установці 13.09: пакет поставився НЕ в /Applications, а в
# теку, де лежала інша копія застосунку (`dist/` після локальної збірки). У
# лозі це видно рядком «PackageKit: Registered bundle file:///…/dist/Device
# Handler.app/». Причина — типове поводження pkgbuild: бандли він позначає
# `BundleIsRelocatable`, і installd через Spotlight шукає вже наявну копію з
# тим самим CFBundleIdentifier та кладе вміст ПОВЕРХ НЕЇ, де б вона не була.
#
# Для нас це вада, а не зручність: шлях зашитий і в LaunchAgent, і в кнопку
# видалення, і в перевірку «вже встановлено». Досить, щоб у людини колись
# полежала копія в Downloads — і установка «успішна», а менеджер не працює.
#
# Лікується описом компонента: беремо той, що pkgbuild склав би сам
# (`--analyze`), і вимикаємо в ньому переміщення.
echo "==> pkgbuild --analyze (опис компонента)"
pkgbuild --analyze --root "$WORKDIR/root" "$WORKDIR/component.plist"
/usr/libexec/PlistBuddy -c "Set :0:BundleIsRelocatable false" "$WORKDIR/component.plist"
/usr/libexec/PlistBuddy -c "Print :0:BundleIsRelocatable" "$WORKDIR/component.plist" | grep -qx false \
  || { echo "::error::не вдалося вимкнути BundleIsRelocatable"; exit 1; }

echo "==> pkgbuild ($VERSION)"
pkgbuild --root "$WORKDIR/root" \
  --component-plist "$WORKDIR/component.plist" \
  --scripts "$SCRIPTS" \
  --identifier "$IDENTIFIER" \
  --version "$VERSION" \
  --install-location / \
  "$WORKDIR/app.pkg"

echo "==> productbuild (майстер: вітання, прогрес, фінал)"
# --package-path: distribution посилається на app.pkg по імені.
productbuild --distribution "$DIST" \
  --resources "$RESOURCES" \
  --package-path "$WORKDIR" \
  "$WORKDIR/unsigned.pkg"

installer_identity="$(security find-identity -v 2>/dev/null \
  | grep 'Developer ID Installer' | head -n1 | sed -E 's/.*"(.*)".*/\1/' || true)"

mkdir -p "$(dirname "$PKG")"
if [ -n "$installer_identity" ]; then
  echo "==> Підписую пакет як: $installer_identity"
  # productsign, а не codesign: пакети підписуються іншим інструментом і
  # іншим сертифікатом. codesign на .pkg «спрацює», але Gatekeeper такий
  # підпис не приймає — саме на цьому легко втратити пів дня.
  productsign --sign "$installer_identity" --timestamp "$WORKDIR/unsigned.pkg" "$PKG"
  pkgutil --check-signature "$PKG" | head -3
else
  echo "==> Сертифіката Developer ID Installer немає — пакет лишається непідписаним"
  cp "$WORKDIR/unsigned.pkg" "$PKG"
fi

if [ -n "$installer_identity" ] && [ -n "${MAC_NOTARY_APPLE_ID:-}" ] \
   && [ -n "${MAC_NOTARY_PASSWORD:-}" ] && [ -n "${MAC_NOTARY_TEAM_ID:-}" ]; then
  echo "==> Нотаризація пакета (чекаю вердикт Apple)"
  xcrun notarytool submit "$PKG" \
    --apple-id "$MAC_NOTARY_APPLE_ID" \
    --password "$MAC_NOTARY_PASSWORD" \
    --team-id "$MAC_NOTARY_TEAM_ID" \
    --wait --timeout 20m
  echo "==> Приклеюю тікет (staple)"
  xcrun stapler staple "$PKG"
  echo "==> Перевірка очима Gatekeeper"
  spctl -a -t install -v "$PKG"
else
  echo "==> Нотаризацію пакета пропущено (немає MAC_NOTARY_* або підпису)"
fi

echo "==> Готово: $PKG"
