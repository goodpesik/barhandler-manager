#!/bin/bash
#
# BH-150 — postinstall для macOS-пакета.
#
# Що він мусить зробити, і чому кожен крок тут:
#
# 1. Зареєструвати LaunchAgent і ПІДНЯТИ його зараз. У .dmg-версії застосунок
#    реєстрував автозапуск сам, але той починав діяти лише з наступного входу
#    в систему — тобто людина ставила менеджер і мусила перелогінитись, щоб
#    він працював «як належить». Інсталятор такого питання не ставить.
# 2. Зробити це для КОРИСТУВАЧА, а не для root: пакет ставиться з правами
#    адміністратора, і `launchctl bootstrap gui/0` завантажив би агент у
#    сесію root, де немає ні дашборда, ні доступу до теки даних людини.
#    Тому визначаємо, хто справді сидить за машиною ($USER пакета — root).
#
# stdout/stderr пакета видно в /var/log/install.log — туди й пишемо.
set -u

LABEL="com.goodpesik.barhandler-manager"

# $3 — том, на який ставлять. Майже завжди "/", але при розгортанні образу
# (installer -target /Volumes/Other, MDM-провізіонінг) це інший том, і тоді
# наші шляхи інші. Доти тут стояло жорстке /Applications — знайдено ревʼю:
# перевірка нижче падала б, і така установка звалилась би без причини.
TARGET="${3:-/}"
TARGET="${TARGET%/}"  # "/" -> "", "/Volumes/X/" -> "/Volumes/X"

APP="$TARGET/Applications/Device Handler.app/Contents/MacOS/bhm"
# Стара назва бандла — з установок до BH-150. Її треба знести, інакше дві
# копії будуть воювати за порт 9999.
OLD_APP="$TARGET/Applications/BarhandlerManager.app"

# ПЕРШЕ — переконатись, що новий застосунок на місці. Порядок тут не косметика:
# спершу цей скрипт зносив стару копію й лише потім перевіряв нову, тож будь-яка
# невдача розпакування лишала машину взагалі без менеджера — гірше, ніж було до
# установки (знайдено ревʼю).
if [ ! -x "$APP" ]; then
  echo "device-handler: застосунку немає за шляхом $APP — установка неповна"
  exit 1
fi

# Стара копія — до розгалужень, але вже після перевірки вище. Спершу це стояло
# аж у гілці «за машиною хтось є», і установка по SSH або через MDM (де нижче
# стоїть exit 0) лишала старий бандл на місці: два агенти, обидва чекають
# порт 9999, працює випадковий (знайдено ревʼю).
if [ -d "$OLD_APP" ]; then
  echo "device-handler: зношу стару копію $OLD_APP"
  rm -rf "$OLD_APP"
  # Її агент указує на шлях, якого вже немає. Системний прибираємо тут,
  # користувацький — нижче, разом із реєстрацією нового.
  rm -f "$TARGET/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
fi

# Ставлять на інший том (розгортання образу, MDM). Підняти агент зараз
# неможливо: launchd керує сесіями ЦІЄЇ системи, а не тієї, що на диску. Тому
# кладемо агент ДЛЯ ВСІХ на той том — launchd підхопить його при першому вході
# після завантаження з нього.
#
# Доти тут стояв лише exit 0 із приміткою «застосунок зареєструє автозапуск
# сам». Знайдено ревʼю: це неправда. ensure_launch_agent() у застосунку
# виконується, лише коли його ХТОСЬ запустив, а в безлюдному розгортанні
# запускати нікому — автозапуск не налаштувався б узагалі.
#
# Шлях у plist — від кореня ТІЄЇ системи (/Applications/...), без $TARGET:
# після завантаження той том і буде "/".
if [ -n "$TARGET" ]; then
  SYS_PLIST="$TARGET/Library/LaunchAgents/$LABEL.plist"
  install -d -m 755 "$TARGET/Library/LaunchAgents"
  cat > "$SYS_PLIST" <<ALT_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>/Applications/Device Handler.app/Contents/MacOS/bhm</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
ALT_EOF
  chown root:wheel "$SYS_PLIST" 2>/dev/null || true
  chmod 644 "$SYS_PLIST"
  echo "device-handler: том $TARGET — агент для всіх покладено в $SYS_PLIST"
  exit 0
fi

# Хто зараз у графічній сесії. `stat /dev/console` — єдиний надійний спосіб:
# $USER тут root, $SUDO_USER не заданий (пакет ставить installd, не sudo),
# а `logname` під installd може не мати tty.
CONSOLE_USER="$(stat -f "%Su" /dev/console 2>/dev/null || echo "")"
if [ -z "$CONSOLE_USER" ] || [ "$CONSOLE_USER" = "root" ]; then
  # Нікого не залогінено: установка по SSH, через MDM або на екрані входу.
  # Доти тут був просто `exit 0` — тихий провал: агент не зареєстрований,
  # ніхто про це не дізнається, а фінальний екран обіцяє, що менеджер уже
  # працює (знайдено ревʼю).
  #
  # Тому кладемо АГЕНТ ДЛЯ ВСІХ у /Library/LaunchAgents: launchd підхопить
  # його при вході будь-якого користувача. Права root у пакета є, а для
  # POS-машини «агент для того, хто сяде за неї» — саме те, що треба.
  echo "device-handler: нікого не залогінено — ставлю агент для всіх користувачів"
  SYS_PLIST="$TARGET/Library/LaunchAgents/$LABEL.plist"
  cat > "$SYS_PLIST" <<SYS_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>$APP</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
SYS_EOF
  chown root:wheel "$SYS_PLIST"
  chmod 644 "$SYS_PLIST"
  echo "device-handler: агент для всіх покладено в $SYS_PLIST — стартує при наступному вході"
  exit 0
fi

# Хтось за машиною є — ставимо агент саме йому, а не всім. І прибираємо
# агент «для всіх», якщо він лишився з headless-установки: інакше дві копії
# воювали б за порт 9999.
rm -f "$TARGET/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
CONSOLE_UID="$(id -u "$CONSOLE_USER")"
HOME_DIR="$(eval echo "~$CONSOLE_USER")"
PLIST="$HOME_DIR/Library/LaunchAgents/$LABEL.plist"
DATA_DIR="$HOME_DIR/.barhandler-manager"

echo "device-handler: реєструю агент для $CONSOLE_USER (uid=$CONSOLE_UID)"

install -d -o "$CONSOLE_USER" -m 755 "$HOME_DIR/Library/LaunchAgents"
install -d -o "$CONSOLE_USER" -m 755 "$DATA_DIR"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>$APP</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$DATA_DIR/bhm.out.log</string>
    <key>StandardErrorPath</key><string>$DATA_DIR/bhm.err.log</string>
</dict>
</plist>
PLIST_EOF
chown "$CONSOLE_USER" "$PLIST"
chmod 644 "$PLIST"

# ─── підняти агент у сесії КОРИСТУВАЧА ────────────────────────────────────
#
# BH-155. Цей скрипт виконує `installd`, тобто ми root. А агент має жити в
# графічній сесії користувача, і `launchctl` із root-контексту туди не
# дістає — саме тому оновлення 0.5.3 → 0.5.7 лишило машину власника без
# менеджера: у лозі «bootstrap не вдався», а той самий рядок з оболонки
# користувача проходив із кодом 0.
#
# `launchctl asuser <uid> launchctl …` виконує команду всередині сесії того
# користувача. Це НЕ офіційно підтримана дорога — інженери Apple самі радять
# замість неї SMAppService, — але з postinstall іншої немає, і саме вона
# працює. Тому нижче ми не віримо жодній «успішній» відповіді на слово, а
# перевіряємо результат запитом до менеджера.
as_user() { launchctl asuser "$CONSOLE_UID" launchctl "$@"; }

# Живий процес менеджера — за обома назвами бандла (нова й до BH-150) і ЛИШЕ
# того користувача, якому ми ставимо. Ми root, тож без `-u` і `pgrep`, і
# `pkill` бачили б процеси ВСІХ користувачів: при швидкому перемиканні
# користувачів оновлення для одного вбивало б менеджер іншого. В install.sh
# цього обмеження немає й воно там не потрібне — той скрипт працює від самого
# користувача, і система сама не дасть йому чужі процеси (знайдено ревʼю).
manager_running() {
  pgrep -u "$CONSOLE_UID" -f "Device Handler.app/Contents/MacOS/bhm" >/dev/null 2>&1 ||
    pgrep -u "$CONSOLE_UID" -f "BarhandlerManager.app/Contents/MacOS/bhm" >/dev/null 2>&1
}

# ── BH-164: ПЕРЕД тим як зупиняти менеджер, спитати, чи він не посеред оплати ──
#
# ЦЕЙ БЛОК ДУБЛЮЄТЬСЯ в `install.sh` і `mac-postinstall.sh` дослівно. Спільного
# файлу тут бути не може: кожен зі скриптів доставляється сам по собі (один
# завантажують одним рядком із GitHub, другий лежить усередині .pkg). Правиш в
# одному — правиш в обох; тест `test_the_two_installer_copies_have_not_drifted`
# порівнює їх посимвольно й падає на розбіжності.
#
# Чому цього не досить перевірити в `/system/update`: там перевірку роблять у мить
# натискання кнопки, а сюди доходить тоді, коли людина пройде майстер установки —
# через хвилину або через двадцять. За цей час може початись оплата карткою, і
# `bootout` + `pkill -9` нижче зняли б менеджер посеред SSI-обміну: картку
# списано, а каса результату не дізнається ніколи. Скрипт до того ж запускають і
# руками, без жодного endpoint'а.
#
# Перервати установку тут уже не можна — файли покладені. Тому ЧЕКАЄМО. Стеля —
# 240 с: у SSI-адаптері опитування стану обмежене 180 с (STATUS_POLL_MAX_S), тож
# реальна оплата за цей час або добігає, або падає своїм таймаутом. Не
# звільнився — ставимо далі: вічно висіти в інсталяторі гірше.
#
# ВІДПОВІДЬ «мене немає» = «вільно». Не запущений менеджер, стара версія без
# /busy, відсутній curl — усе це не має блокувати установку, інакше ми зробили б
# оновлення неможливим саме там, де воно найпотрібніше. А ось ТАЙМАУТ — інша
# річ: порт слухають, але не відповідають, і це може бути саме той менеджер, що
# зараз друкує. Його чекаємо, див. `manager_busy`.
manager_busy() {
    command -v curl >/dev/null 2>&1 || return 1
    _bh_body=$(curl -fsS --max-time 3 http://127.0.0.1:9999/busy 2>/dev/null)
    _bh_rc=$?
    if [ "$_bh_rc" -eq 0 ]; then
        printf '%s' "$_bh_body" | grep -q '"busy"[[:space:]]*:[[:space:]]*true'
        return $?
    fi
    # Код 28 — ТАЙМАУТ: менеджер слухає порт, але за 3 с не відповів. Це не те
    # саме, що «його немає» (7 — не змогли підключитись) чи «стара версія»
    # (22 — відповів помилкою HTTP). Друге коло ревʼю: синхронний USB-запис
    # блокує весь цикл подій, тобто менеджер, який ЗАРАЗ друкує або платить,
    # може не встигнути відповісти — і мовчання виглядало б як «вільно», хоч це
    # найгірший момент для вбивства. Тому таймаут трактуємо як «зайнятий»:
    # почекаємо, а не вбʼємо. Стеля нижче все одно скінчиться.
    [ "$_bh_rc" -eq 28 ]
}

wait_until_manager_free() {
    manager_busy || return 0
    echo "device-handler: менеджер зайнятий (оплата або друк) — чекаю"
    _bh_waited=0
    while [ "$_bh_waited" -lt 240 ]; do
        manager_busy || break
        sleep 3
        _bh_waited=$((_bh_waited + 3))
    done
    if manager_busy; then
        echo "device-handler: менеджер досі зайнятий після ${_bh_waited}с — продовжую"
    else
        echo "device-handler: менеджер звільнився за ${_bh_waited}с"
    fi
}
# ── кінець спільного блоку BH-164 ──

wait_until_manager_free


# Знімаємо старий екземпляр і ЧЕКАЄМО, поки він справді зникне. Без очікування
# наступний bootstrap ловить «service already loaded» і нічого не робить, а
# працює далі попередня копія — тобто оновлення не оновлює.
as_user bootout "gui/$CONSOLE_UID/$LABEL" >/dev/null 2>&1 || true

# Чекаємо і на службу, і на ПРОЦЕС. Дивитись лише на `launchctl print` мало з
# двох причин: uvicorn завершується не миттєво (5+ секунд), і сама команда
# може впасти зі своєї причини — тоді цикл вийшов би одразу й нічого не
# чекав (знайдено ревʼю). Процес видно незалежно від launchd.
for _ in $(seq 1 15); do
  as_user print "gui/$CONSOLE_UID/$LABEL" >/dev/null 2>&1 || manager_running || break
  sleep 1
done

# Не пішов сам — знімаємо силою. Інакше він тримає порт 9999, новий екземпляр
# не може його зайняти, а `bootstrap` при цьому звітує успіх. Так само робить
# install.sh для скриптової інсталяції.
if manager_running; then
  echo "device-handler: старий процес не завершився — знімаю"
  pkill -9 -u "$CONSOLE_UID" -f "Device Handler.app/Contents/MacOS/bhm" 2>/dev/null || true
  pkill -9 -u "$CONSOLE_UID" -f "BarhandlerManager.app/Contents/MacOS/bhm" 2>/dev/null || true
  sleep 1
fi

booted=0
for attempt in 1 2 3; do
  if as_user bootstrap "gui/$CONSOLE_UID" "$PLIST" >/dev/null 2>&1; then
    booted=1
    break
  fi
  echo "device-handler: спроба $attempt підняти агент не вдалася, повторюю"
  # Перед повтором знімаємо те, що могло зареєструватись напівдорозі: інакше
  # наступна спроба впаде на «already loaded», а працювати буде стара
  # реєстрація зі старим шляхом.
  as_user bootout "gui/$CONSOLE_UID/$LABEL" >/dev/null 2>&1 || true
  sleep 2
done

# `kickstart` тут НЕ викликаємо навмисно. Він перезапускає те, що вже
# завантажене в домен, і НЕ перечитує plist з диска. Якщо ми сюди дійшли з
# `booted=0`, найімовірніша причина — стара реєстрація, яку не зняв bootout;
# kickstart підняв би саме її, тобто СТАРУ версію, і звітував успіх. Рівно та
# вада, проти якої цей тікет (знайдено ревʼю).

# ─── перевірка результату ─────────────────────────────────────────────────
#
# Питаємо не «чи хтось відповідає», а «чи відповідає ПОТРІБНА версія». Стара
# копія, яка ще не добила SIGTERM, теж віддає /health — і тоді успіх був би
# неправдою. Те саме правило вже діє в install.sh; сюди його не переносили, і
# це знайшло ревʼю.
#
# `__PKG_VERSION__` підставляє scripts/mac_build_pkg.sh із файла VERSION.
WANT_VERSION="__PKG_VERSION__"

up=0
served=""       # остання відповідь
answered=""     # БУДЬ-ЯКА відповідь за весь цикл
for _ in $(seq 1 30); do
  served="$(curl -fsS --max-time 1 http://localhost:9999/health 2>/dev/null || true)"
  if [ -n "$served" ]; then
    answered="$served"
    if [ -z "${WANT_VERSION##*_PKG_VERSION_*}" ]; then
      # Версію не підставили (скрипт запустили поза збіркою) — тоді
      # задовольняємось самою відповіддю, але кажемо про це прямо.
      up=1
      break
    fi
    case "$served" in
      *"\"version\":\"$WANT_VERSION\""*) up=1; break ;;
    esac
  fi
  sleep 1
done

if [ "$up" -eq 1 ]; then
  echo "device-handler: менеджер $WANT_VERSION працює на http://localhost:9999"
elif [ -n "$answered" ]; then
  # Дивимось на будь-яку відповідь за цикл, а не лише на останню: стара копія
  # могла відповісти кілька разів і замовкнути перед 30-ю спробою — тоді вирок
  # «не відповів» ховав би те, що ми насправді бачили (знайдено ревʼю).
  echo "device-handler: на порті 9999 відповідає ІНША версія — схоже, стара копія не завершилась"
  echo "device-handler: перевірте $DATA_DIR/bhm.err.log і перезайдіть у систему"
elif [ "$booted" -eq 1 ]; then
  echo "device-handler: агент піднято, але менеджер не відповів за 30 с — перевірте $DATA_DIR/bhm.err.log"
else
  echo "device-handler: НЕ ВДАЛОСЯ підняти менеджер; він стартує при наступному вході в систему"
  echo "device-handler: підняти зараз: launchctl bootstrap gui/$CONSOLE_UID $PLIST"
fi

exit 0
