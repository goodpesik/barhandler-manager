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
APP="/Applications/BarhandlerManager.app/Contents/MacOS/bhm"

# Хто зараз у графічній сесії. `stat /dev/console` — єдиний надійний спосіб:
# $USER тут root, $SUDO_USER не заданий (пакет ставить installd, не sudo),
# а `logname` під installd може не мати tty.
CONSOLE_USER="$(stat -f "%Su" /dev/console 2>/dev/null || echo "")"
if [ -z "$CONSOLE_USER" ] || [ "$CONSOLE_USER" = "root" ]; then
  echo "device-handler: не вдалося визначити користувача графічної сесії — агент не реєструю"
  exit 0   # НЕ валимо установку: застосунок сам зареєструє автозапуск при першому запуску
fi
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
