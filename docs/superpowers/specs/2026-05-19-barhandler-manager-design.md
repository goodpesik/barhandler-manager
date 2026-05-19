# Barhandler Manager — Design Spec
_2026-05-19_

## Overview

Local HTTP server (`localhost:9999`) that bridges web apps (BarHandler, FitStudio, future products) with physical hardware. Runs on the client machine alongside the printer. Web apps communicate via REST API — they never deal with hardware details directly.

**Repo:** github.com/goodpesik/barhandler-manager  
**Stack:** Python 3.11+, FastAPI, uvicorn, pyusb, python-escpos, pyyaml, pydantic v2

---

## Architecture — Layered (Approach B)

```
barhandler-manager/
├── main.py                  # entry point, loads config, starts uvicorn
├── config.yaml              # device config (USB IDs, paper width, API key)
├── requirements.txt
└── src/
    ├── config.py            # config.yaml loader
    ├── server.py            # FastAPI app, auth middleware, router registration
    ├── routes/
    │   ├── health.py        # GET /health (no auth)
    │   ├── devices.py       # GET /devices, GET /devices/scan
    │   ├── print.py         # POST /print/receipt
    │   └── drawer.py        # POST /drawer/open
    ├── services/
    │   └── receipt.py       # JSON → ESC/POS rendering (58mm)
    ├── devices/
    │   └── printer.py       # USB/LAN connection + asyncio FIFO queue
    └── models/
        └── receipt.py       # Pydantic request models
```

**Data flow:**
```
Web app → POST /print/receipt
  → auth middleware (X-Api-Key)
  → route handler
  → receipt service (renders ESC/POS bytes)
  → printer device (enqueues job)
  → asyncio.Queue (FIFO) → prints → response 200
```

---

## Config (`config.yaml`)

```yaml
server:
  port: 9999
  api_key: "change-me"

devices:
  receipt:
    enabled: true
    connection: usb          # usb | network
    vendor_id: null          # e.g. 0x04b8 (Epson), set after USB scan
    product_id: null         # e.g. 0x0202
    # network alternative:
    # host: "192.168.1.100"
    # port: 9100
    paper_width: 58          # mm — this printer is 58mm

  label:
    enabled: false           # Phase 2
    protocol: tspl           # tspl | zpl

  terminal:
    enabled: false           # Phase 2 — Monobank acquiring, Ethernet
    host: null
    port: null
```

---

## API

### `GET /health` — no auth required
```json
{
  "status": "ok",
  "version": "0.1.0",
  "devices": {
    "receipt": "connected",
    "label": "not_configured",
    "terminal": "not_configured"
  }
}
```
Web app polls this on login and every 30 min. If unreachable — shows "Пристрій недоступний" modal.

### `GET /devices` — list USB devices (for setup UI in web app)
```json
{
  "devices": [
    { "vendor_id": "0x04b8", "product_id": "0x0202" }
  ]
}
```

### `POST /print/receipt`
Request:
```json
{
  "header": "BarHandler — Кафе Ромашка",
  "items": [
    { "name": "Бурбон Jack Daniel's", "qty": 2, "price": 120.0 },
    { "name": "Реберця", "qty": 1, "price": 185.0 }
  ],
  "total": 425.0,
  "payment": "cash",
  "footer": "Дякуємо за візит!",
  "open_drawer": true
}
```
- `header` / `footer` — optional, provided by web app (each product has its own branding)
- `open_drawer` — optional bool, default false; silently ignored if no drawer connected
- Response waits until physically printed (synchronous, FIFO queue)

Response:
```json
{ "status": "printed" }
```

### `POST /drawer/open` — open cash drawer independently
```json
{ "status": "opened" }
```
Silently succeeds if no drawer connected (no error).

### `POST /terminal/charge` — Phase 2 stub
```json
{ "status": "not_implemented" }
```
Monobank acquiring terminal over Ethernet. Protocol TBD in Phase 2.

---

## Key Decisions

| Topic | Decision |
|---|---|
| Payload format | Structured JSON — server renders ESC/POS |
| Auth | `X-Api-Key` header; `/health` exempt |
| Print queue | `asyncio.Queue` FIFO per device, response after print |
| Cash drawer | Separate endpoint + optional flag in receipt; graceful if absent |
| Paper width | 58mm (E 582 R printer) |
| Connection types | USB and LAN/Ethernet both supported |
| Label printer | Phase 2 (TSPL/ZPL TBD by model) |
| POS terminal | Phase 2 — Monobank, Ethernet |
| Packaging | Phase 3 — PyInstaller → exe/dmg (libusb bundling needed) |

---

## Phase Roadmap

**Phase 1 (current):** Server infrastructure + receipt printer (USB/LAN, ESC/POS, 58mm) + cash drawer  
**Phase 2:** Label printer (TSPL/ZPL) + Monobank POS terminal (Ethernet)  
**Phase 3:** Packaging — PyInstaller → Windows exe + macOS dmg (note: Windows needs Zadig for WinUSB driver, one-time setup)

---

## Error Handling

- Printer not connected → `503 Service Unavailable` `{ "error": "printer_unavailable" }`
- Invalid API key → `401 Unauthorized`
- Malformed request → `422 Unprocessable Entity` (Pydantic auto)
- Cash drawer absent → silent success (not an error)
- Print job fails mid-queue → `500` with error detail, queue continues
