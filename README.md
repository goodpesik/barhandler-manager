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

**POS-термінали (багатобанкова підтримка)**
- Один уніфікований інтерфейс, свій адаптер на кожен ECR-протокол:

  | Банк(и) | Протокол | Порт | `kind` |
  |---|---|---|---|
  | Monobank | SSI ECR JSON (Servus) | 3000 | `mono_pos` |
  | будь-який SSI-термінал | SSI ECR JSON | 3000 | `generic_ssi` |
  | ПриватБанк | PrivatBank ECR JSON | 2000 | `privat_pos` |
  | Райффайзен / ПУМБ | Verifone Printec PosAPI | 8080¹ | `raif_pos` / `pumb_pos` |
  | Банк Південний / Sense (Альфа) | BPOS1 / BPOS Light | 8888¹ | `pivdenny_pos` / `sense_pos` |
  | Ощадбанк | Oschad ECR | 7777¹ | `oschad_pos` |

  ¹ Printec/BPOS/Ощад інтегруються через локальний **міст** (Printec-сервіс / СОТА Агент / банківський ECR-драйвер) поруч із менеджером — адаптер вказує на `host:port` мосту.
- Discover у LAN по всіх портах, реєстрація прямо з дашборду, мультимерчантні термінали з псевдонімами
- Проведення оплат, скасування, парсинг фіскального ID для ПриватБанку з активованою "Касою"
- **Увага:** «бренд банку ≠ один протокол». Райф/ПУМБ переважно Printec PosAPI, але частина Android-юнітів говорить SSI — такий реєструйте як `generic_ssi`. Wire-формати Printec/BPOS/Ощад — партнерські (не публічні); наші адаптери — узгоджена модель, перевірена локальним емулятором (`python -m emulator`), місця під реальні доки позначені `# SPEC:`.

**Веб-дашборд**
- `http://localhost:9999/` — live статус принтерів і терміналів, без авторизації

### Підтримуване обладнання

| Тип | Протокол | Ширина паперу |
|---|---|---|
| Чекові принтери | ESC/POS | 58 мм, 80 мм |
| Лейбл принтери | ESC/POS | 48 мм, 58 мм |
| POS-термінали | SSI ECR / PrivatBank JSON / Printec PosAPI / BPOS / Oschad ECR | — |

Протестовано на: STMicro-class 58 мм USB, Epson TM-i (мережа), Xprinter XP-246B (48 мм USB лейбл). ZPL/TSPL принтери (Zebra, TSC) не підтримуються.

### Встановлення

#### macOS / Linux / Raspberry Pi

```bash
curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.sh | bash
```

#### Windows

Встановлення виконується інсталятором **[barhandler-setup.exe](https://github.com/goodpesik/barhandler-manager/releases/latest/download/barhandler-setup.exe)** — прав адміністратора та Python не потребує.

Інсталятор автоматично видаляє попередню версію (зокрема стару Python-версію та її завдання автозапуску), встановлює поточну начисто, налаштовує автозапуск при вході в систему та додає запис до розділу «Програми та засоби» для подальшого видалення. Друк по USB виконується через стандартний драйвер принтера Windows (принтер лишається доступним для інших програм), USB-термінал ПриватБанку працює через віртуальний COM-порт. Оновлення застосовуються кнопкою **«⬆ Оновити»** на дашборді.

<details><summary>Альтернатива — Python-версія через PowerShell</summary>

```powershell
irm https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.ps1 | iex
```

Ставить Python + менеджер у `~/.barhandler-manager/` як Scheduled Task. USB-друк і USB-термінал у цій версії обмежені — для них бери `.exe`-інсталятор.

</details>

#### Android (Termux)

```bash
curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/install-android.sh | bash
```

Скриптові інсталери (macOS / Linux / Android та Windows-PowerShell): ставлять Python 3.11+, розпаковують менеджер у `~/.barhandler-manager/`, створюють virtualenv, ставлять залежності та реєструють службу автозапуску (launchd на macOS, systemd на Linux, termux-services на Android, Scheduled Task на Windows). Windows-`.exe` — самодостатній, Python не потрібен.

---

#### 🍏 Докладно, крок за кроком — macOS

1. **Відкрий Термінал.** Натисни `Cmd` + `Пробіл` (відкриється пошук Spotlight), набери `Terminal` або `Термінал`, натисни `Enter`. *(Альтернатива: Finder → Програми → Утиліти → Термінал.)* Відкриється чорне/біле вікно з текстовим рядком.
2. **Встав команду встановлення.** Скопіюй рядок нижче повністю, встав у Термінал (`Cmd` + `V`) і натисни `Enter`:
   ```bash
   curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.sh | bash
   ```
3. **Що відбувається далі:** скрипт завантажує менеджер у теку `~/.barhandler-manager/`, за потреби ставить Python, створює службу автозапуску. У Терміналі побігтимуть рядки логу — це нормально, чекай.
4. **Якщо спитає пароль** (`Password:`) — це пароль твого користувача Mac (той, яким входиш у систему). Введи його й натисни `Enter`. **Символи не показуються під час набору — це нормально**, просто друкуй наосліп.
5. **Дозволи мережі.** macOS може показати вікно *«…хоче приймати вхідні з'єднання»* — натисни **Дозволити / Allow**. Воно з'явиться поверх екрана (може також блимнути іконкою в доку).
6. **Готово.** Відкрий браузер і зайди на **http://localhost:9999/** — має відкритися дашборд менеджера. Термінал можна закривати, менеджер працює у фоні й сам стартує після перезавантаження.

#### 🪟 Докладніше — Windows

1. **Завантаження інсталятора.** Файл [barhandler-setup.exe](https://github.com/goodpesik/barhandler-manager/releases/latest/download/barhandler-setup.exe) зберігається до теки «Завантаження».
2. **Встановлення.** Запуск `barhandler-setup.exe` → **Далі → Встановити**. Якщо з'явиться попередження *«Windows захистив ваш ПК» (SmartScreen)* — оберіть **«Докладніше» → «Виконати в будь-якому разі»**: файл не має цифрового підпису, це очікувано. Права адміністратора не потрібні.
3. **Завершення.** Інсталятор видаляє попередню версію (за наявності), встановлює поточну, вмикає автозапуск і запускає менеджер; наприкінці відкривається дашборд на **http://localhost:9999/**.

> **Примітка.** Якщо дашборд не відкрився одразу — зачекайте 10–20 секунд (перший старт триваліший) і оновіть сторінку. Видалення — через **Пуск → Параметри → Програми → BarHandler Manager → Видалити** (або «Програми та засоби»).

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
| `/devices/register-usb-manual` | POST | Зареєструвати USB-принтер за VID/PID/ендпоінтами (коли скан не бачить). |
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
| `/terminal/register-manual` | POST | Зареєструвати термінал за IP/портом/банком (коли скан не бачить). |
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

**POS terminals (multi-bank)**
- One unified interface, a dedicated adapter per ECR protocol:

  | Bank(s) | Protocol | Port | `kind` |
  |---|---|---|---|
  | Monobank | SSI ECR JSON (Servus) | 3000 | `mono_pos` |
  | any SSI terminal | SSI ECR JSON | 3000 | `generic_ssi` |
  | PrivatBank | PrivatBank ECR JSON | 2000 | `privat_pos` |
  | Raiffeisen / PUMB | Verifone Printec PosAPI | 8080¹ | `raif_pos` / `pumb_pos` |
  | Bank Pivdenny / Sense (Alfa) | BPOS1 / BPOS Light | 8888¹ | `pivdenny_pos` / `sense_pos` |
  | Oschadbank | Oschad ECR | 7777¹ | `oschad_pos` |

  ¹ Printec/BPOS/Oschad integrate through a local **bridge** (Printec service / СОТА Агент / bank ECR driver) next to the manager — the adapter points at the bridge's `host:port`.
- LAN discovery across all ports, register straight from the dashboard, multi-merchant terminals with nicknames
- Charges, cancellation, fiscal-ID parsing for PrivatBank merchants with "Каса" activated
- **Note:** "bank brand ≠ one protocol". Raif/PUMB are mostly Printec PosAPI, but some Android units speak SSI — register those as `generic_ssi`. The Printec/BPOS/Oschad wire formats are partner-gated (not public); our adapters are a best-effort model validated by the bundled emulator (`python -m emulator`), with real-doc gaps marked `# SPEC:`.

**Web dashboard**
- `http://localhost:9999/` — live printer and terminal status, no auth required

### Supported hardware

| Type | Protocol | Paper width |
|---|---|---|
| Receipt printers | ESC/POS | 58 mm, 80 mm |
| Label printers | ESC/POS | 48 mm, 58 mm |
| POS terminals | SSI ECR / PrivatBank JSON / Printec PosAPI / BPOS / Oschad ECR | — |

Tested on: STMicro-class 58 mm USB, Epson TM-i (network), Xprinter XP-246B (48 mm USB label). ZPL/TSPL printers (Zebra, TSC) are not supported.

### Install

#### macOS / Linux / Raspberry Pi

```bash
curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.sh | bash
```

#### Windows

Installation is performed by the **[barhandler-setup.exe](https://github.com/goodpesik/barhandler-manager/releases/latest/download/barhandler-setup.exe)** installer — it requires neither administrator rights nor Python.

The installer automatically removes any previous version (including the old Python install and its auto-start task), installs the current one clean, configures start-up at logon, and adds an entry to "Apps & features" for later removal. USB printing goes through the standard Windows printer driver (the printer stays available to other applications); the PrivatBank USB terminal works over a virtual COM port. Updates are applied with the **"⬆ Update"** button on the dashboard.

<details><summary>Alternative — Python version via PowerShell</summary>

```powershell
irm https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.ps1 | iex
```

Installs Python + the manager into `~/.barhandler-manager/` as a Scheduled Task. USB printing and the USB terminal are limited in this build — use the `.exe` installer for those.

</details>

#### Android (Termux)

```bash
curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/install-android.sh | bash
```

The script installers (macOS / Linux / Android and Windows-PowerShell): install Python 3.11+, unpack the manager to `~/.barhandler-manager/`, create a virtualenv, install dependencies, and register an auto-start service (launchd on macOS, systemd on Linux, termux-services on Android, Scheduled Task on Windows). The Windows `.exe` is self-contained — no Python needed.

After install: `http://localhost:9999/` for the dashboard, `http://localhost:9999/health` for liveness.

---

#### 🍏 Step by step — macOS

1. **Open Terminal.** Press `Cmd` + `Space` (Spotlight), type `Terminal`, press `Enter`. *(Or Finder → Applications → Utilities → Terminal.)* A window with a text prompt opens.
2. **Paste the install command.** Copy the line below in full, paste it into Terminal (`Cmd` + `V`), press `Enter`:
   ```bash
   curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.sh | bash
   ```
3. **What happens next:** the script downloads the manager into `~/.barhandler-manager/`, installs Python if needed, and registers the auto-start service. Log lines scroll by — that's normal, wait for it.
4. **If it asks for a password** (`Password:`) that's your Mac login password. Type it and press `Enter`. **The characters don't appear as you type — that's normal**, just type it blind.
5. **Network permission.** macOS may show *"…wants to accept incoming connections"* — click **Allow**. It appears on top of the screen (may also bounce an icon in the Dock).
6. **Done.** Open a browser at **http://localhost:9999/** — the dashboard should load. You can close Terminal; the manager runs in the background and starts itself after a reboot.

#### 🪟 More detail — Windows

1. **Download.** The [barhandler-setup.exe](https://github.com/goodpesik/barhandler-manager/releases/latest/download/barhandler-setup.exe) file is saved to the Downloads folder.
2. **Installation.** Run `barhandler-setup.exe` → **Next → Install**. If a *"Windows protected your PC" (SmartScreen)* prompt appears, choose **"More info" → "Run anyway"**: the file is unsigned, which is expected. Administrator rights are not required.
3. **Completion.** The installer removes any previous version, installs the current one, enables auto-start, and launches the manager; the dashboard opens at **http://localhost:9999/**.

> **Note.** If the dashboard doesn't open immediately, wait 10–20 seconds (the first start is slower) and refresh. Uninstall via **Start → Settings → Apps → BarHandler Manager → Uninstall** (or "Apps & features").

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
| `/devices/register-usb-manual` | POST | Register a USB printer by VID/PID/endpoints (when the scan can't see it). |
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
| `/terminal/register-manual` | POST | Register a terminal by IP/port/bank (when the scan can't see it). |
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
