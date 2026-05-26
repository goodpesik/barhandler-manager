# barhandler-manager

> 🇺🇦 **Українською нижче** / **[English below](#english)**

---

## Українська

Локальний HTTP-шлюз, який дозволяє веб-касі (BarHandler, FitStudio або
будь-якій іншій, що вміє JSON over HTTP) керувати термопринтером,
грошовою скринькою або POS-терміналом, фізично підключеними до тієї ж
машини. Працює на `localhost:9999`.

Браузер сам по собі не має доступу до USB / serial — менеджер стоїть
посередині: веб-додаток шле запит на друк/оплату → менеджер говорить з
обладнанням → повертає результат. Один невеликий Python-сервіс на
касовій машині обслуговує все підключене залізо.

### Що вміє зараз

- **Веб-дашборд** — на `http://localhost:9999/` відкривається live
  dashboard зі статусом підключених принтерів, POS-терміналів та
  останніх операцій. Не потребує авторизації.
- **Друк чеків** — фіскальний (стиль Вчасно), нефіскальний, рахунок
  для гостя перед оплатою, кухонна квитанція. Форматування по рядках
  (жирний, по центру, подвійна висота) — щоб око касира одразу
  чіплялося за номер замовлення / СУМА.
- **Кирилиця, яка реально друкується** — кожен рядок растеризується
  через Noto Sans Mono і відсилається як `GS v 0` raster image. Працює
  на будь-якому ESC/POS принтері незалежно від того, які code pages
  підтримує його прошивка.
- **Грошова скринька** — імпульс на drawer-kick роз’єм принтера після
  продажу (опційно).
- **Виявлення пристроїв** — `POST /devices/discover` знаходить USB
  принтерного класу, mDNS-броузить IPP / `_pdl-datastream`, port-scan
  по власному /24 для raw-9100. Bluetooth — best-effort на Linux
  (`bluetoothctl`); спершу спаруйте принтер в ОС, тоді він з’явиться.
- **POS-термінали** — повна підтримка Monobank (SSI ECR JSON) і
  ПриватБанк (PB ECR JSON). Discover у LAN на портах 3000 (SSI) та
  2000 (PrivatBank), реєстрація, мультимерчантні термінали з
  псевдонімами, проведення оплат, парсинг фіскальних ID для
  ПриватБанку з активованою "Касою". Деталі: `docs/INTEGRATION-SPEC.md`.

### Підтримуване обладнання

Будь-що з ESC/POS — протестовано на STMicroelectronics-класі 58 мм
USB-принтерах та Epson TM-i по мережі. 58 мм та 80 мм папір.
Етикеточні принтери (TSPL / ZPL) — Phase 2.

### Встановлення

#### macOS / Linux / Raspberry Pi

```bash
curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.sh | bash
```

#### Windows

```powershell
irm https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.ps1 | iex
```

#### Android (Termux)

```bash
curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/install-android.sh | bash
```

Усі три інсталери роблять одне й те саме: ставлять Python 3.11+ якщо
його ще нема, розпаковують менеджер у `~/.barhandler-manager/`,
створюють virtualenv, ставлять залежності та **реєструють службу яка
автоматично стартує при кожному завантаженні машини** (launchd на
macOS, systemd на Linux, termux-services на Android, Scheduled Task на
Windows).

Після встановлення менеджер доступний за адресою `http://localhost:9999`.
Відкрийте `http://localhost:9999/` у браузері — побачите дашборд зі
статусом пристроїв.

### Що буде після перезавантаження компʼютера?

**Нічого робити не треба** — менеджер запуститься сам:

- **macOS** — `RunAtLoad=true` + `KeepAlive=true`: стартує при логіні
  користувача і автоматично перезапускається, якщо процес впав
- **Linux** — `systemctl enable` + `Restart=on-failure`: стартує при
  завантаженні системи, перезапускається при збоях
- **Android (Termux)** — `sv-enable`: стартує при відкритті Termux
  (для постійної роботи у фоні потрібен Termux:Boot з F-Droid)
- **Windows** — Scheduled Task `-AtLogOn`: стартує при вході
  користувача в систему

Перевірити що менеджер працює:

```bash
curl http://localhost:9999/health
# {"status": "ok", ...}
```

### Ручне керування

Після встановлення в `~/.barhandler-manager/` зявляються 4 скрипти:

| Скрипт | Що робить |
|---|---|
| `start.sh` / `start.ps1` | Запустити менеджер вручну |
| `stop.sh` / `stop.ps1` | Зупинити менеджер |
| `status.sh` / `status.ps1` | Показати стан (запущено / зупинено + порт) |
| `update.sh` / `update.ps1` | Оновитись до останньої версії з GitHub Releases |

Приклад:

```bash
~/.barhandler-manager/status.sh
~/.barhandler-manager/stop.sh
~/.barhandler-manager/start.sh
~/.barhandler-manager/update.sh
```

### Консоль оператора (CLI)

Окремо є компактна CLI для операторського використання — підключається до
запущеного менеджера по HTTP і показує живий dashboard зі станом
принтерів і POS-терміналів:

```bash
.venv/bin/python cli.py             # живий dashboard (default)
.venv/bin/python cli.py start       # запуск у detached-режимі (виживає
                                    #   при закритті терміналу/SSH)
.venv/bin/python cli.py stop        # зупинка
.venv/bin/python cli.py restart     # stop + start
.venv/bin/python cli.py status      # те саме що без аргументів
.venv/bin/python cli.py logs        # tail -F bhm.log
.venv/bin/python cli.py health      # one-shot health-перевірка (exit code)
```

`cli.py start` ставить процес у власну сесію (POSIX `start_new_session`),
тож менеджер переживе закриття консолі. PID зберігається у `bhm.pid`,
логи в `bhm.log` поруч з `main.py`.

⚠️ **Авто-рестарт при крашi** CLI **НЕ робить** — для production-рівневої
надійності (старт при ребуті + рестарт при крашi + survives logout)
користуйтесь інсталером вище (launchd на macOS / systemd на Linux).

### Налаштування

Файл `config.yaml` поруч з `main.py`:

```yaml
server:
  port: 9999                 # змінити якщо 9999 зайнятий
  api_key: "bf11b47b-..."    # усі роути крім /health та / вимагають це в X-Api-Key
  cors_origins:              # точні origins для localhost dev-серверів
    - "http://localhost:4115"
    - "http://localhost:5273"
  cors_origin_regex: "https://([a-z0-9-]+\\.)?(barhandler\\.com|petshandler\\.com|fitstudiocrm\\.com)"
```

- **`api_key`** — статичний токен, який фронтенд передає в хедері
  `X-Api-Key`. Не секрет у класичному сенсі — просто handshake щоб
  сторонній софт на хості не міг випадково відкрити скриньку чи
  надрукувати чек. Однаковий для всіх POS-додатків.
- **`cors_origins`** — точний список дозволених origins (localhost
  dev-сервери).
- **`cors_origin_regex`** — regex для мультитенантних продакшн-доменів.
  Матчить будь-який субдомен: `[client].barhandler.com`,
  `[client].petshandler.com`, `[client].fitstudiocrm.com` та їхні
  `.web.app` деплої. Працює разом з `cors_origins` — достатньо збігу
  в будь-якому з них.

Усе інше (ширина паперу принтера, drawer pin, code page) налаштовується
зі **сторінки Settings веб-додатку**, не з цього файлу — менеджер сам
виявляє USB / LAN принтери, оператор реєструє їх через UI, і призначення
зберігаються в `printers.json` поруч з менеджером.

### API коротко

| Endpoint | Метод | Що робить |
|---|---|---|
| `/` | GET | Веб-дашборд — live статус принтерів і терміналів. Без auth. |
| `/health` | GET | Liveness + статус кожного принтера (JSON). Без auth. |
| `/devices/discover` | POST | Скан USB + LAN (+ Bluetooth на Linux). |
| `/devices` | GET | Список зареєстрованих принтерів. |
| `/devices/register` | POST | Зареєструвати принтер з role / nickname / шириною паперу. |
| `/devices/{id}` | DELETE | Видалити принтер з реєстру. |
| `/devices/{id}/test-print` | POST | Демо-чек. |
| `/print/fiscal` | POST | Фіскальний чек у стилі Вчасно з QR-кодом. |
| `/print/receipt` | POST | Нефіскальний чек. |
| `/print/lines` | POST | Структуровані рядки з форматуванням по рядку. |
| `/print/text` | POST | Сирий заздалегідь сформатований текст (вихід Checkbox `/text`). |
| `/print/kitchen` | POST | Кухонна квитанція — один самодостатній блок на позицію. |
| `/drawer/open` | POST | Імпульс на грошову скриньку. |
| `/terminal/discover` | POST | Скан LAN для POS-терміналів (порти 3000 SSI + 2000 PB). |
| `/terminal/register` | POST | Зареєструвати термінал. |
| `/terminal` | GET | Список зареєстрованих терміналів. |
| `/terminal/{id}/merchants` | GET / PUT | Список мерчантів + апдейт псевдонімів. |
| `/terminal/charge` | POST | Провести оплату (банк визначається з реєстрації). |
| `/terminal/{id}/cancel` | POST | Скасувати поточну операцію. |
| `/terminal/{id}/last-result` | GET | Отримати результат по UID або останній. |

Повні схеми payload-ів — у `docs/INTEGRATION-SPEC.md` (документ, з
якого читає веб-додаток коли підключає виклики).

### Встановлення вручну (для розробки)

```bash
git clone https://github.com/goodpesik/barhandler-manager.git
cd barhandler-manager
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

### Релізи

Гілка `main` — щоденна; релізи відвантажуються з `production` через
GitHub Releases. До кожного релізу прикріплюється:

- Tarball з вихідниками
- `install.sh`, `install.ps1`, `install-android.sh`

Авто-оновлення нема — перезапустіть інсталер (або `update.sh`) коли
зявиться новий реліз. Налаштування (`printers.json`, `terminals.json`,
`config.yaml`) переживають оновлення.

### Ліцензія

MIT.

### Контриб'ютинг

Issues і PR вітаються. Для повідомлень про hardware-баги наведіть
vendor:product принтера (з `lsusb` / `system_profiler SPUSBDataType`) і
відповідні рядки з `bhm.log`.

---

## English

Local HTTP bridge that lets a browser-based POS (BarHandler, FitStudio,
or anything else that can talk JSON over HTTP) drive a thermal printer,
cash drawer, or POS terminal that's physically connected to the same
machine. Runs on `localhost:9999`.

The browser by itself can't reach USB / serial hardware, so the manager
sits in the middle: web app sends a print/charge request → manager
talks to the device → returns the result. One small Python service on
the bar/till machine, drives every piece of hardware on it.

### What it does today

- **Web dashboard** — open `http://localhost:9999/` in a browser to see
  a live dashboard with printer and POS terminal status. No auth
  required.
- **Receipt printing** — fiscal layout (Vchasno-style), non-fiscal,
  pre-payment bill, kitchen ticket. Per-line formatting (bold,
  centred, double-height) so the operator's eye lands on the things
  that matter (order number, СУМА).
- **Cyrillic that actually prints** — every line is rasterised through
  Noto Sans Mono and emitted as a `GS v 0` raster image. Works on any
  ESC/POS printer regardless of which code pages its firmware exposes.
- **Cash drawer** — pulses the drawer-kick connector on the printer
  after a sale (configurable).
- **Device discovery** — `POST /devices/discover` finds USB
  printer-class devices, browses mDNS for IPP / `_pdl-datastream`
  printers, and port-scans the host's own /24 for raw-9100 listeners.
  Bluetooth is best-effort on Linux (scrapes `bluetoothctl`) — pair
  the printer in your OS first, then it shows up.
- **POS terminal** — full Monobank (SSI ECR JSON) and PrivatBank
  (PB ECR JSON) support. LAN discovery on ports 3000 (SSI) + 2000 (PB),
  registration, multi-merchant terminals with nicknames, card
  charging, and fiscal-ID parsing for PrivatBank merchants with
  "Каса" activated. Details in `docs/INTEGRATION-SPEC.md`.

### Supported hardware

Anything that speaks ESC/POS — tested on STMicroelectronics-class
58 mm USB printers and Epson TM-i over network. 58 mm and 80 mm paper
both supported. Label printers (TSPL / ZPL) — Phase 2.

### Install

#### macOS / Linux / Raspberry Pi

```bash
curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.sh | bash
```

#### Windows

```powershell
irm https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.ps1 | iex
```

#### Android (Termux)

```bash
curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/install-android.sh | bash
```

All three installers do the same thing: install Python 3.11+ if it's
missing, drop the manager under `~/.barhandler-manager/`, create a
virtualenv, install dependencies, and **register a service that starts
automatically on every boot** (launchd on macOS, systemd on Linux,
termux-services on Android, Scheduled Task on Windows).

After install the manager is up at `http://localhost:9999`.
Open `http://localhost:9999/` in a browser to see the device dashboard.

### What happens after a reboot?

**Nothing for you to do** — the manager comes back up on its own:

- **macOS** — `RunAtLoad=true` + `KeepAlive=true`: starts at user
  login and auto-restarts if the process dies
- **Linux** — `systemctl enable` + `Restart=on-failure`: starts at
  system boot, restarts on failures
- **Android (Termux)** — `sv-enable`: starts when Termux opens (for
  persistent background, install Termux:Boot from F-Droid)
- **Windows** — Scheduled Task `-AtLogOn`: starts at user logon

Verify it's running:

```bash
curl http://localhost:9999/health
# {"status": "ok", ...}
```

### Manual control

The installer drops 4 helper scripts under `~/.barhandler-manager/`:

| Script | What it does |
|---|---|
| `start.sh` / `start.ps1` | Start the manager manually |
| `stop.sh` / `stop.ps1` | Stop the manager |
| `status.sh` / `status.ps1` | Show state (running / stopped + port) |
| `update.sh` / `update.ps1` | Update to the latest GitHub Releases version |

Example:

```bash
~/.barhandler-manager/status.sh
~/.barhandler-manager/stop.sh
~/.barhandler-manager/start.sh
~/.barhandler-manager/update.sh
```

### Operator CLI

A compact operator console talks to a running manager over HTTP and
shows a live dashboard of printers and POS terminals:

```bash
.venv/bin/python cli.py             # live dashboard (default)
.venv/bin/python cli.py start       # detached launch (survives
                                    #   shell / SSH close)
.venv/bin/python cli.py stop        # stop
.venv/bin/python cli.py restart     # stop + start
.venv/bin/python cli.py status      # same as no-arg
.venv/bin/python cli.py logs        # tail -F bhm.log
.venv/bin/python cli.py health      # one-shot health check (exit code)
```

`cli.py start` puts the process in its own POSIX session
(`start_new_session`), so the manager survives the controlling shell
closing. PID lands in `bhm.pid`, logs in `bhm.log` next to `main.py`.

⚠️ **Auto-restart on crash is NOT handled by this CLI** — for
production-grade resilience (boot start + crash restart + survives
logout) use the installer above (launchd on macOS / systemd on Linux).

### Manual install (for development)

```bash
git clone https://github.com/goodpesik/barhandler-manager.git
cd barhandler-manager
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

### Configuration

`config.yaml` next to `main.py`:

```yaml
server:
  port: 9999                 # change if 9999 is taken
  api_key: "bf11b47b-..."    # all routes except /health and / require this in X-Api-Key
  cors_origins:              # exact origins for localhost dev servers
    - "http://localhost:4115"
    - "http://localhost:5273"
  cors_origin_regex: "https://([a-z0-9-]+\\.)?(barhandler\\.com|petshandler\\.com|fitstudiocrm\\.com)"
```

- **`api_key`** — static token the frontend sends in `X-Api-Key`.
  Not a secret per se — just a handshake so random software on the
  host can't accidentally open the cash drawer or print a receipt.
  Same key across all POS apps.
- **`cors_origins`** — exact list of allowed origins (localhost dev
  servers).
- **`cors_origin_regex`** — regex for multi-tenant production domains.
  Matches any subdomain: `[client].barhandler.com`,
  `[client].petshandler.com`, `[client].fitstudiocrm.com` and their
  `.web.app` deploys. Works together with `cors_origins` — a match in
  either is enough.

Everything else (printer paper width, drawer pin, code page) is
configured from the **web app's Settings page**, not this file — the
manager auto-detects USB / LAN printers, the operator registers them
through the UI, and the assignments are stored in `printers.json`
beside the manager.

### API at a glance

| Endpoint | Method | What it does |
|---|---|---|
| `/` | GET | Web dashboard — live printer and terminal status. No auth. |
| `/health` | GET | Liveness + per-printer status (JSON). No auth. |
| `/devices/discover` | POST | Scan USB + LAN (+ Bluetooth on Linux). |
| `/devices` | GET | List registered printers. |
| `/devices/register` | POST | Persist a printer with role / nickname / paper width. |
| `/devices/{id}` | DELETE | Unregister a printer. |
| `/devices/{id}/test-print` | POST | Friendly demo receipt. |
| `/print/fiscal` | POST | Vchasno-style fiscal receipt with QR code. |
| `/print/receipt` | POST | Non-fiscal receipt. |
| `/print/lines` | POST | Structured lines with per-line bold / align / double-height. |
| `/print/text` | POST | Raw pre-formatted text (Checkbox `/text` endpoint output). |
| `/print/kitchen` | POST | Kitchen ticket — one self-contained block per item. |
| `/drawer/open` | POST | Pulse the cash drawer. |
| `/terminal/discover` | POST | LAN scan for POS terminals (ports 3000 SSI + 2000 PB). |
| `/terminal/register` | POST | Register a terminal. |
| `/terminal` | GET | List registered terminals. |
| `/terminal/{id}/merchants` | GET / PUT | Merchant list + nickname update. |
| `/terminal/charge` | POST | Run a charge (bank inferred from registration). |
| `/terminal/{id}/cancel` | POST | Interrupt the in-flight operation. |
| `/terminal/{id}/last-result` | GET | Fetch result by UID or last completed. |

Full payload schemas live in `docs/INTEGRATION-SPEC.md` (the doc the
web-app side reads when wiring its calls).

### Releases

`main` is the day-to-day branch; releases ship from `production` via
GitHub Releases. Every release attaches:

- Source tarball
- `install.sh`, `install.ps1`, `install-android.sh`

Auto-update isn't implemented — re-run the installer (or `update.sh`)
when a new release lands. Settings (`printers.json`, `terminals.json`,
`config.yaml`) survive upgrades.

### License

MIT.

### Contributing

Issues and PRs welcome. For hardware-specific bug reports include the
printer's vendor:product (from `lsusb` / `system_profiler
SPUSBDataType`) and the relevant lines from `bhm.log`.
