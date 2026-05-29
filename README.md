# barhandler-manager

> 🇺🇦 **Українською нижче** / **[English below](#english)**

---

## Українська

Локальний HTTP-шлюз між браузерним POS-додатком і залізом на тій самій машині: термопринтерами, грошовою скринькою та POS-терміналами. Браузер не має прямого доступу до USB/serial — менеджер стоїть посередині і отримує команди по JSON. Працює на `localhost:9999`.

### Що вміє

**Друк**
- Чекові принтери 58 мм та 80 мм (ESC/POS), лейбл принтери 48 мм та 58 мм (ESC/POS)
- Кирилиця на будь-якому ESC/POS принтері — кожен рядок растеризується через Noto Sans Mono і відсилається як `GS v 0` raster bitmap, тому code pages прошивки принтера не важливі
- Фіскальний чек у стилі Вчасно з QR-кодом (`/print/fiscal`)
- Нефіскальний чек (`/print/receipt`)
- Попередній рахунок / структуровані рядки з форматуванням по рядку — жирний, центр, подвійна висота (`/print/lines`)
- Сирий заздалегідь сформатований текст — вихід Checkbox `/text` (`/print/text`)
- Кухонна квитанція — один самодостатній блок на позицію, відривний формат (`/print/kitchen`)
- Лейбл — готове зображення (base64 PNG), автомасштаб до ширини паперу, без відрізу (`/print/label`)
- Грошова скринька через drawer-kick роз'єм (`/drawer/open`)

**Виявлення та реєстрація пристроїв**
- `POST /devices/discover` — USB принтерного класу, mDNS (IPP / `_pdl-datastream`), port-scan /24 на raw-9100, Bluetooth best-effort на Linux
- Принтери реєструються через UI (роль, псевдонім, ширина паперу), зберігаються у `printers.json`

**POS-термінали**
- Monobank SSI ECR JSON (порт 3000) та ПриватБанк PB ECR JSON (порт 2000)
- Discover у LAN, реєстрація, мультимерчантні термінали з псевдонімами
- Проведення оплат, скасування, парсинг фіскального ID для ПриватБанку з активованою "Касою"

**Веб-дашборд**
- `http://localhost:9999/` — live статус принтерів і терміналів, без авторизації

### Підтримуване обладнання

| Тип | Протокол | Ширина паперу |
|---|---|---|
| Чекові принтери | ESC/POS | 58 мм, 80 мм |
| Лейбл принтери | ESC/POS | 48 мм, 58 мм |
| POS-термінали | SSI ECR / PB ECR | — |

Протестовано на: STMicro-class 58 мм USB, Epson TM-i (мережа), Xprinter XP-246B (48 мм USB лейбл). ZPL/TSPL принтери (Zebra, TSC) не підтримуються.

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

Всі три інсталери: ставлять Python 3.11+, розпаковують менеджер у `~/.barhandler-manager/`, створюють virtualenv, ставлять залежності та реєструють службу автозапуску (launchd на macOS, systemd на Linux, termux-services на Android, Scheduled Task на Windows).

Після встановлення: `http://localhost:9999/` — дашборд, `http://localhost:9999/health` — liveness.

### Автозапуск після перезавантаження

Нічого робити не треба — менеджер стартує сам:

| Платформа | Механізм |
|---|---|
| macOS | launchd `RunAtLoad=true` + `KeepAlive=true` |
| Linux | systemd `enable` + `Restart=on-failure` |
| Android (Termux) | sv-enable (для фону потрібен Termux:Boot з F-Droid) |
| Windows | Scheduled Task `-AtLogOn` |

### Ручне керування

```bash
~/.barhandler-manager/status.sh   # стан (запущено / зупинено + порт)
~/.barhandler-manager/start.sh    # запустити вручну
~/.barhandler-manager/stop.sh     # зупинити
~/.barhandler-manager/update.sh   # оновитись до останньої версії
```

На Windows: ті самі назви з `.ps1`.

### CLI

```bash
.venv/bin/python cli.py             # живий dashboard (default)
.venv/bin/python cli.py start       # detached-запуск (виживає при закритті терміналу)
.venv/bin/python cli.py stop
.venv/bin/python cli.py restart
.venv/bin/python cli.py logs        # tail -F bhm.log
.venv/bin/python cli.py health      # one-shot перевірка (exit code)
```

`cli.py start` — процес у власній POSIX-сесії, PID у `bhm.pid`, логи у `bhm.log`. Авто-рестарт при краші CLI не робить — для production використовуйте інсталер (launchd / systemd).

### Налаштування

`config.yaml` поруч з `main.py`:

```yaml
server:
  port: 9999
  api_key: "bf11b47b-..."       # X-Api-Key на всіх роутах крім / і /health
  cors_origins:
    - "http://localhost:4115"
    - "http://localhost:5273"
  cors_origin_regex: "https://([a-z0-9-]+\\.)?(barhandler\\.com|petshandler\\.com|fitstudiocrm\\.com)"
```

- **`api_key`** — статичний handshake-токен; не секрет в класичному сенсі, просто щоб сторонній процес на хості не відкрив скриньку випадково.
- **`cors_origin_regex`** — матчить будь-який субдомен barhandler.com / petshandler.com / fitstudiocrm.com та їхні `.web.app` деплої.

Ширина паперу, drawer pin, code page — налаштовуються через UI веб-додатку і зберігаються у `printers.json`.

### API

| Endpoint | Метод | Що робить |
|---|---|---|
| `/` | GET | Веб-дашборд. Без auth. |
| `/health` | GET | Liveness + статус пристроїв (JSON). Без auth. |
| `/devices/discover` | POST | Скан USB + LAN + Bluetooth. |
| `/devices` | GET | Список зареєстрованих принтерів. |
| `/devices/register` | POST | Зареєструвати принтер (роль / псевдонім / ширина паперу). |
| `/devices/{id}` | DELETE | Видалити з реєстру. |
| `/devices/{id}/test-print` | POST | Демо-чек. |
| `/print/receipt` | POST | Нефіскальний чек. |
| `/print/fiscal` | POST | Фіскальний чек (Вчасно-стиль) з QR-кодом. |
| `/print/text` | POST | Сирий текст (вихід Checkbox `/text`). |
| `/print/lines` | POST | Структуровані рядки з форматуванням. |
| `/print/kitchen` | POST | Кухонна квитанція. |
| `/print/label` | POST | Лейбл — base64 PNG, автомасштаб, без відрізу. |
| `/drawer/open` | POST | Імпульс на грошову скриньку. |
| `/terminal/discover` | POST | Скан LAN для POS-терміналів. |
| `/terminal/register` | POST | Зареєструвати термінал. |
| `/terminal` | GET | Список зареєстрованих терміналів. |
| `/terminal/{id}/merchants` | GET / PUT | Мерчанти + псевдоніми. |
| `/terminal/charge` | POST | Провести оплату. |
| `/terminal/{id}/cancel` | POST | Скасувати поточну операцію. |
| `/terminal/{id}/last-result` | GET | Результат по UID або останній. |

Повні схеми payload-ів — `docs/INTEGRATION-SPEC.md`.

### Встановлення вручну (для розробки)

```bash
git clone https://github.com/goodpesik/barhandler-manager.git
cd barhandler-manager
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

### Релізи

`main` — щоденна розробка; релізи з `production` через GitHub Releases. Налаштування (`printers.json`, `terminals.json`, `config.yaml`) переживають оновлення. Запустіть `update.sh` коли зʼявиться новий реліз.

### Ліцензія

MIT.

### Контриб'ютинг

Issues і PR вітаються. Для hardware-багів вкажіть vendor:product принтера (`lsusb` / `system_profiler SPUSBDataType`) і відповідні рядки з `bhm.log`.

---

## English

Local HTTP bridge between a browser-based POS app and hardware on the same machine: thermal printers, cash drawer, POS terminals. The browser has no direct USB/serial access — the manager sits in the middle, taking JSON commands. Runs on `localhost:9999`.

### What it does

**Printing**
- Receipt printers 58 mm and 80 mm (ESC/POS), label printers 48 mm and 58 mm (ESC/POS)
- Cyrillic on any ESC/POS printer — every line is rasterised through Noto Sans Mono and sent as a `GS v 0` bitmap, so the printer firmware's code page support doesn't matter
- Fiscal receipt (Vchasno layout) with QR code (`/print/fiscal`)
- Non-fiscal receipt (`/print/receipt`)
- Pre-payment bill / structured lines with per-line formatting — bold, centre, double-height (`/print/lines`)
- Raw pre-formatted text — Checkbox `/text` output (`/print/text`)
- Kitchen ticket — one self-contained tear-off block per item (`/print/kitchen`)
- Label — pre-rendered image (base64 PNG), auto-scaled to paper dot width, no cut (`/print/label`)
- Cash drawer pulse via drawer-kick connector (`/drawer/open`)

**Device discovery and registration**
- `POST /devices/discover` — USB printer-class, mDNS (IPP / `_pdl-datastream`), /24 port-scan on raw-9100, Bluetooth best-effort on Linux
- Printers registered through the web UI (role, nickname, paper width), stored in `printers.json`

**POS terminals**
- Monobank SSI ECR JSON (port 3000) and PrivatBank PB ECR JSON (port 2000)
- LAN discovery, registration, multi-merchant terminals with nicknames
- Charges, cancellation, fiscal-ID parsing for PrivatBank merchants with "Каса" activated

**Web dashboard**
- `http://localhost:9999/` — live printer and terminal status, no auth required

### Supported hardware

| Type | Protocol | Paper width |
|---|---|---|
| Receipt printers | ESC/POS | 58 mm, 80 mm |
| Label printers | ESC/POS | 48 mm, 58 mm |
| POS terminals | SSI ECR / PB ECR | — |

Tested on: STMicro-class 58 mm USB, Epson TM-i (network), Xprinter XP-246B (48 mm USB label). ZPL/TSPL printers (Zebra, TSC) are not supported.

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

All three installers: install Python 3.11+, unpack the manager to `~/.barhandler-manager/`, create a virtualenv, install dependencies, and register an auto-start service (launchd on macOS, systemd on Linux, termux-services on Android, Scheduled Task on Windows).

After install: `http://localhost:9999/` for the dashboard, `http://localhost:9999/health` for liveness.

### Auto-start after reboot

Nothing to do — the manager comes back up on its own:

| Platform | Mechanism |
|---|---|
| macOS | launchd `RunAtLoad=true` + `KeepAlive=true` |
| Linux | systemd `enable` + `Restart=on-failure` |
| Android (Termux) | sv-enable (persistent background needs Termux:Boot from F-Droid) |
| Windows | Scheduled Task `-AtLogOn` |

### Manual control

```bash
~/.barhandler-manager/status.sh   # state (running / stopped + port)
~/.barhandler-manager/start.sh    # start manually
~/.barhandler-manager/stop.sh     # stop
~/.barhandler-manager/update.sh   # update to the latest release
```

Windows: same names with `.ps1`.

### CLI

```bash
.venv/bin/python cli.py             # live dashboard (default)
.venv/bin/python cli.py start       # detached launch (survives shell/SSH close)
.venv/bin/python cli.py stop
.venv/bin/python cli.py restart
.venv/bin/python cli.py logs        # tail -F bhm.log
.venv/bin/python cli.py health      # one-shot health check (exit code)
```

`cli.py start` puts the process in its own POSIX session; PID in `bhm.pid`, logs in `bhm.log`. Auto-restart on crash is not handled by the CLI — for production use the installer (launchd / systemd).

### Configuration

`config.yaml` next to `main.py`:

```yaml
server:
  port: 9999
  api_key: "bf11b47b-..."       # X-Api-Key on all routes except / and /health
  cors_origins:
    - "http://localhost:4115"
    - "http://localhost:5273"
  cors_origin_regex: "https://([a-z0-9-]+\\.)?(barhandler\\.com|petshandler\\.com|fitstudiocrm\\.com)"
```

- **`api_key`** — static handshake token; not a secret in the traditional sense, just prevents random processes on the host from accidentally opening the drawer.
- **`cors_origin_regex`** — matches any subdomain of barhandler.com / petshandler.com / fitstudiocrm.com and their `.web.app` deploys.

Paper width, drawer pin, code page — configured through the web app UI and stored in `printers.json`.

### API

| Endpoint | Method | What it does |
|---|---|---|
| `/` | GET | Web dashboard. No auth. |
| `/health` | GET | Liveness + device status (JSON). No auth. |
| `/devices/discover` | POST | Scan USB + LAN + Bluetooth. |
| `/devices` | GET | List registered printers. |
| `/devices/register` | POST | Register a printer (role / nickname / paper width). |
| `/devices/{id}` | DELETE | Unregister a printer. |
| `/devices/{id}/test-print` | POST | Demo receipt. |
| `/print/receipt` | POST | Non-fiscal receipt. |
| `/print/fiscal` | POST | Fiscal receipt (Vchasno layout) with QR code. |
| `/print/text` | POST | Raw pre-formatted text (Checkbox `/text` output). |
| `/print/lines` | POST | Structured lines with per-line formatting. |
| `/print/kitchen` | POST | Kitchen ticket. |
| `/print/label` | POST | Label — base64 PNG, auto-scaled, no cut. |
| `/drawer/open` | POST | Pulse the cash drawer. |
| `/terminal/discover` | POST | LAN scan for POS terminals. |
| `/terminal/register` | POST | Register a terminal. |
| `/terminal` | GET | List registered terminals. |
| `/terminal/{id}/merchants` | GET / PUT | Merchant list + nickname update. |
| `/terminal/charge` | POST | Run a charge. |
| `/terminal/{id}/cancel` | POST | Cancel the in-flight operation. |
| `/terminal/{id}/last-result` | GET | Fetch result by UID or last completed. |

Full payload schemas in `docs/INTEGRATION-SPEC.md`.

### Manual install (for development)

```bash
git clone https://github.com/goodpesik/barhandler-manager.git
cd barhandler-manager
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

### Releases

`main` is the day-to-day branch; releases ship from `production` via GitHub Releases. Settings (`printers.json`, `terminals.json`, `config.yaml`) survive upgrades. Run `update.sh` when a new release lands.

### License

MIT.

### Contributing

Issues and PRs welcome. For hardware-specific bug reports include the printer's vendor:product (from `lsusb` / `system_profiler SPUSBDataType`) and the relevant lines from `bhm.log`.
