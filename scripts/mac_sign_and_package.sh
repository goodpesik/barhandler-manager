#!/usr/bin/env bash
#
# Підписати BarhandlerManager.app, запакувати в .dmg і (якщо є креденшели)
# пронотаризувати його в Apple. Кличуть обидва мак-джоби:
#   .github/workflows/publish.yml        — релізний канал
#   .github/workflows/build-exe-dev.yml  — нічний канал
#
#   scripts/mac_sign_and_package.sh <шлях до .app> <шлях до .dmg> [шлях до .pkg]
#
# Третій аргумент необовʼязковий: якщо він є, поряд із .dmg збирається ще й
# .pkg-інсталятор (BH-150). Збирається ТУТ, а не окремим кроком, свідомо —
# productsign шукає сертифікат Developer ID Installer у keychain, а вона
# тимчасова й живе лише до виходу з цього скрипта. Окремий крок у workflow
# знайшов би порожньо й тихо віддав непідписаний пакет.
#
# Два режими, і вибирає їх наявність секретів, а не прапорець:
#
#   ПОВНИЙ — є MAC_CERT_P12_BASE64 + MAC_CERT_PASSWORD: підпис сертифікатом
#   Developer ID Application з hardened runtime, міткою часу й entitlements.
#   Якщо додатково є MAC_NOTARY_* — .dmg їде в нотаризацію і отримує staple,
#   після чого відкривається подвійним кліком без жодних питань.
#
#   AD-HOC — секретів немає (форк, чужа гілка): `codesign --sign -`. Це знімає
#   жорстке блокування «damaged» на Apple Silicon, але лишає «unidentified
#   developer»: перший запуск через right-click → Open. Збірка НЕ падає —
#   інакше кожен форк ловив би червоне CI на рівному місці.
#
# Документація до секретів: docs/macos-signing.md
set -euo pipefail

APP="${1:?перший аргумент — шлях до .app}"
DMG="${2:?другий аргумент — шлях до .dmg на виході}"
PKG="${3:-}"
VOLNAME="${MAC_DMG_VOLNAME:-Barhandler Manager}"
ENTITLEMENTS="${MAC_ENTITLEMENTS:-installers/entitlements-mac.plist}"

[ -d "$APP" ] || { echo "::error::немає $APP"; exit 1; }

# Агент автозапуску кладемо В БАНДЛ і ДО підпису: SMAppService реєструє лише
# те, що лежить у Contents/Library/LaunchAgents і накрите підписом
# застосунку. Саме через це система показує в «Автозапуск і розширення»
# «Device Handler», а не команду сертифіката (BH-150).
AGENT_SRC="${MAC_LAUNCHAGENT:-installers/mac-launchagent.plist}"
if [ -f "$AGENT_SRC" ]; then
  echo "==> Кладу агент у бандл: $(basename "$AGENT_SRC")"
  mkdir -p "$APP/Contents/Library/LaunchAgents"
  cp "$AGENT_SRC" "$APP/Contents/Library/LaunchAgents/com.goodpesik.barhandler-manager.plist"
fi

KEYCHAIN=""
WORKDIR=""
P12=""
STAGE=""
cleanup() {
  # Зносимо ЗАВЖДИ — і на помилці теж. Розшифрований .p12 тут головний: якщо
  # `security import` упаде (хибний пароль, битий base64), `set -e` перерве
  # скрипт, і без цього рядка приватний ключ лишився б лежати у $RUNNER_TEMP
  # (знайдено ревʼю — попередній варіант прибирав лише keychain).
  [ -n "$KEYCHAIN" ] && security delete-keychain "$KEYCHAIN" 2>/dev/null || true
  [ -n "$WORKDIR" ] && rm -rf "$WORKDIR" || true
  [ -n "$STAGE" ] && rm -rf "$STAGE" || true
}
trap cleanup EXIT

sign_identity=""

if [ -n "${MAC_CERT_P12_BASE64:-}" ] && [ -n "${MAC_CERT_PASSWORD:-}" ]; then
  echo "==> Розпаковую сертифікат у тимчасову keychain"
  # mktemp, а не PID у імені: поза CI (де RUNNER_TEMP не заданий) шлях
  # інакше був би передбачуваним у спільному /tmp. Знайдено ревʼю.
  WORKDIR="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/bhm-signing-XXXXXX")"
  chmod 700 "$WORKDIR"
  KEYCHAIN="$WORKDIR/signing.keychain-db"
  KEYCHAIN_PASS="$(openssl rand -hex 24)"
  P12="$WORKDIR/cert.p12"

  printf '%s' "$MAC_CERT_P12_BASE64" | base64 --decode > "$P12"
  chmod 600 "$P12"
  security create-keychain -p "$KEYCHAIN_PASS" "$KEYCHAIN"
  security set-keychain-settings -lut 21600 "$KEYCHAIN"
  security unlock-keychain -p "$KEYCHAIN_PASS" "$KEYCHAIN"
  # -A не ставимо: доступ дається переліченим інструментам, а не всьому, що
  # запуститься на раннері. productsign і productbuild тут ОБОВʼЯЗКОВІ:
  # пакет підписує саме productsign, і без дозволу macOS не падає, а піднімає
  # вікно авторизації — у CI його нікому натиснути, і джоба висить годинами
  # (так і сталось на релізі v0.5.0: 2+ години на кроці підпису).
  security import "$P12" -k "$KEYCHAIN" -P "$MAC_CERT_PASSWORD" \
    -T /usr/bin/codesign -T /usr/bin/security \
    -T /usr/bin/productsign -T /usr/bin/productbuild
  rm -f "$P12"
  # Без цього codesign на кожен підпис підняв би GUI-запит і завис би в CI.
  # productsign: і productbuild: — окремі партиції, і їхня відсутність давала
  # рівно той самий беззвучний зависання на підписі пакета.
  security set-key-partition-list \
    -S apple-tool:,apple:,codesign:,productsign:,productbuild: \
    -s -k "$KEYCHAIN_PASS" "$KEYCHAIN" >/dev/null
  # Додаємо в пошук, не витісняючи системні — інакше зникне доступ до
  # кореневих сертифікатів, і notarytool не зможе перевірити ланцюжок.
  security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | tr -d '"')

  # Ідентичність шукаємо за НАЗВОЮ, а не за порядком: у .p12 з локальної
  # keychain зазвичай лежить ще й «Apple Development», і він для підпису поза
  # магазином не годиться.
  sign_identity="$(security find-identity -v -p codesigning "$KEYCHAIN" \
    | grep 'Developer ID Application' | head -n1 | sed -E 's/.*"(.*)".*/\1/')"
  [ -n "$sign_identity" ] || {
    echo "::error::у .p12 немає сертифіката Developer ID Application"
    security find-identity -v -p codesigning "$KEYCHAIN" || true
    exit 1
  }
  echo "==> Підписую як: $sign_identity"
  codesign --force --timestamp --options runtime \
    --entitlements "$ENTITLEMENTS" \
    --sign "$sign_identity" "$APP"
else
  echo "==> Секретів підпису немає — ad-hoc підпис (буде «unidentified developer»)"
  codesign --force --deep --sign - "$APP"
fi

echo "==> Перевіряю підпис застосунку"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "==> Пакую .dmg"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
mkdir -p "$(dirname "$DMG")"
hdiutil create -volname "$VOLNAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

if [ -n "$sign_identity" ]; then
  # Сам образ теж підписуємо: Gatekeeper перевіряє підпис .dmg ще до того, як
  # людина дістанеться застосунку всередині.
  echo "==> Підписую .dmg"
  codesign --force --timestamp --sign "$sign_identity" "$DMG"
fi

if [ -n "$sign_identity" ] && [ -n "${MAC_NOTARY_APPLE_ID:-}" ] \
   && [ -n "${MAC_NOTARY_PASSWORD:-}" ] && [ -n "${MAC_NOTARY_TEAM_ID:-}" ]; then
  echo "==> Нотаризація (чекаю вердикт Apple)"
  # --wait: без нього джоба завершиться раніше за перевірку, і staple нічого
  # не знайде. Помилку не глушимо: непронотаризований .dmg у релізі — це
  # той самий «unidentified developer», тільки вже під нашим підписом.
  xcrun notarytool submit "$DMG" \
    --apple-id "$MAC_NOTARY_APPLE_ID" \
    --password "$MAC_NOTARY_PASSWORD" \
    --team-id "$MAC_NOTARY_TEAM_ID" \
    --wait --timeout 20m

  echo "==> Приклеюю тікет (staple)"
  xcrun stapler staple "$DMG"

  echo "==> Перевіряю очима Gatekeeper"
  spctl -a -t open --context context:primary-signature -v "$DMG"
else
  echo "==> Нотаризацію пропущено (немає MAC_NOTARY_* або підпис ad-hoc)"
fi

if [ -n "$PKG" ]; then
  echo "==> Збираю .pkg, поки keychain із сертифікатами ще жива"
  bash scripts/mac_build_pkg.sh "$APP" "$PKG"
fi

echo "==> Готово: $DMG${PKG:+ і $PKG}"
