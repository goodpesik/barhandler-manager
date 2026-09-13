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

# Спершу знімаємо старий екземпляр — інакше bootstrap віддасть «service already
# loaded», а працювати далі буде попередня копія з попереднього шляху.
launchctl bootout "gui/$CONSOLE_UID/$LABEL" 2>/dev/null || true
if launchctl bootstrap "gui/$CONSOLE_UID" "$PLIST" 2>&1; then
  echo "device-handler: агент піднято, менеджер працює"
else
  echo "device-handler: bootstrap не вдався — менеджер стартує при наступному вході"
fi

exit 0
