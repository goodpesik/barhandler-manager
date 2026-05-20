# barhandler-manager

Local HTTP bridge that lets a browser-based POS (BarHandler, FitStudio,
or anything else that can talk JSON over HTTP) drive a thermal printer,
cash drawer, or POS terminal that's physically connected to the same
machine. Runs on `localhost:9999`.

The browser by itself can't reach USB / serial hardware, so the manager
sits in the middle: web app sends a print/charge request → manager
talks to the device → returns the result. One small Python service on
the bar/till machine, drives every piece of hardware on it.

## What it does today

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
- **POS terminal (Mono, Privatbank)** — adapter scaffold is in
  `src/routes/terminal.py`. Real adapters land once the bank-side
  protocol docs come through; see `docs/INTEGRATION-SPEC.md`.

## Supported hardware

Anything that speaks ESC/POS — tested on STMicroelectronics-class
58 mm USB printers and Epson TM-i over network. 58 mm and 80 mm paper
both supported. Label printers (TSPL / ZPL) — Phase 2.

## Install

### macOS / Linux / Raspberry Pi
```bash
curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.sh | bash
```

### Windows
```powershell
irm https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.ps1 | iex
```

### Android (Termux)
```bash
curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/install-android.sh | bash
```

All three installers do the same thing: install Python 3.11+ if it's
missing, drop the manager under `~/.barhandler-manager/`, create a
virtualenv, install dependencies, and register a service that starts
on boot (launchd / systemd / Termux services / Windows Scheduled
Task).

After install the manager is up at `http://localhost:9999`.

### Manual install (for development)
```bash
git clone https://github.com/goodpesik/barhandler-manager.git
cd barhandler-manager
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

## Configuration

`config.yaml` next to `main.py`. Two things you'll touch:

```yaml
server:
  port: 9999                 # change if 9999 is taken
  api_key: "change-me"       # all routes except /health require this in X-Api-Key
  cors_origins:              # optional — override the built-in dev/prod allowlist
    - "https://your-pos.example.com"
```

Everything else (printer paper width, drawer pin, code page) is
configured from the **web app's Settings page**, not this file — the
manager auto-detects USB / LAN printers, the operator registers them
through the UI, and the assignments are stored in `printers.json`
beside the manager.

## API at a glance

| Endpoint | Method | What it does |
|---|---|---|
| `/health` | GET | Liveness + per-printer status. No auth. |
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
| `/terminal/*` | (Phase 2) | POS terminal charge / cancel / status. |

Full payload schemas live in `docs/INTEGRATION-SPEC.md` (the doc the
web-app side reads when wiring its calls).

## Releases

`main` is the day-to-day branch; releases ship from `production` via
GitHub Releases. Every release attaches:

- Source tarball
- `install.sh`, `install.ps1`, `install-android.sh`

Auto-update isn't implemented — re-run the installer when a new
release lands. Settings (`printers.json`, `config.yaml`) survive
upgrades.

## License

MIT.

## Contributing

Issues and PRs welcome. For hardware-specific bug reports include the
printer's vendor:product (from `lsusb` / `system_profiler
SPUSBDataType`) and the relevant lines from `bhm.log`.
