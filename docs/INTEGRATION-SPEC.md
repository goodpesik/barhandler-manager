# BarHandler ↔ Barhandler Manager integration spec

How the BarHandler web app drives `barhandler-manager` for receipt /
kitchen printing. **Status:** API is implemented in the manager;
BarHandler-side wiring lands when we start the web integration phase.

## Architecture in one paragraph

The manager runs locally (`http://localhost:9999`). The frontend asks it
to discover physical printers on demand, then registers each one with a
role (`receipt` / `kitchen` / `label`). Each registration gets a stable
ID; the frontend persists the IDs in app settings and reuses them in
every print call. Connections to the printers are lazy — opened on the
first print to that ID and kept warm afterwards.

```
              ┌─────────────────────┐
   user ─────►│ Settings → Printers │
              │  • [Discover]       │  POST /devices/discover
              │  • picks STMicro    │
              │  • role: receipt    │
              │  • nickname: Бар-чек│  POST /devices/register {id, kind, ...}
              └────────┬────────────┘
                       │ saves id in app settings
                       ▼
              ┌─────────────────────┐
              │  Transaction paid   │  POST /print/fiscal?printer_id=...
              │  → POST /print/...  │
              └─────────────────────┘
```

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | no | server + per-printer status pills |
| `POST /devices/discover` | yes | scan USB (+ LAN+BT in Phase 2); returns descriptors with stable `id` |
| `GET /devices` | yes | list currently registered printers |
| `POST /devices/register` | yes | persist `{id, kind, nickname, paper_width, render_mode, code_page, drawer_pin}` |
| `DELETE /devices/{id}` | yes | unregister |
| `POST /devices/{id}/probe-codepage` | yes | native-mode aid — prints labelled cyrillic samples in 6 code pages so the operator picks the readable one |
| `POST /devices/{id}/test-print` | yes | print a friendly demo receipt in the printer's current settings — powers the Settings UI's "залишити цей режим" check |
| `POST /print/receipt[?printer_id=]` | yes | internal/non-fiscal JSON receipt |
| `POST /print/fiscal[?printer_id=]`  | yes | unified fiscal (Checkbox + Vchasno) — Vchasno-PDF look |
| `POST /print/text[?printer_id=]`    | yes | raw text (Checkbox `/api/v1/receipts/{id}/text`) |
| `POST /print/kitchen[?printer_id=]` | yes | kitchen ticket (large item names, no totals) |
| `POST /print/label[?printer_id=]`   | yes | label image (base64 PNG); Windows spooler queue checked before sending, as for `test-print` |

Windows spooler queue check (`POST /devices/{id}/test-print`, `POST /print/label`):
the queue state is read before the job is sent. A queue that cannot print is
refused with HTTP 503 and `detail = {code, message}`; nothing is sent to the
queue. `message` names the queue and the spooler flags.

| Queue state (spooler flags) | `detail.code` |
|---|---|
| `offline`, `not_available`, `work_offline` («Use Printer Offline») | `queue_offline` |
| `paused` | `queue_paused` |
| `paper_out` | `out_of_paper` |
| `door_open` | `cover_open` |
| `error`, `paper_jam`, `paper_problem`, `user_intervention`, `pending_deletion`, `server_unknown` | `queue_error` |

When the queue state cannot be read (spooler call fails), the job is sent and a
warning is logged. `/print/receipt`, `/print/fiscal`, `/print/text`,
`/print/lines`, `/print/kitchen` and `/drawer/open` do not read the queue state.
| `POST /drawer/open[?printer_id=]` | yes | pulse cash drawer |
| `POST /terminal/charge` | yes | Phase 2 stub (Monobank acquiring) |

`printer_id` is **optional**. When omitted, the manager picks the first
registration matching the role implied by the endpoint
(`/print/kitchen` → first `kind=kitchen`, others → first `kind=receipt`).

## Discovery response (sample)

```json
{
  "printers": [
    {
      "id": "f81ba96ad564",
      "transport": "usb",
      "label": "STMicroelectronics USB POS Printer",
      "manufacturer": "STMicroelectronics",
      "product": "USB POS Printer",
      "usb": {
        "vendor_id": 1110,
        "product_id": 2056,
        "in_ep": 129,
        "out_ep": 3,
        "serial": null
      }
    }
  ]
}
```

ID is a 12-character SHA-1 prefix of `transport:vendor:product:serial`
(or `transport:host:port` for network, `transport:mac` for Bluetooth) so
the same physical printer always lands on the same id.

## Registration body

```json
{
  "id": "f81ba96ad564",
  "kind": "receipt",
  "nickname": "Бар-чек",
  "paper_width": 58,
  "code_page": null,
  "drawer_pin": 0
}
```

- `kind`: `receipt` | `kitchen` | `label`
- `paper_width`: `58` (32 chars) or `80` (48 chars)
- `render_mode`:
  - `"bitmap"` (default) — every glyph is rasterised through Noto Sans
    Mono and emitted as a `GS v 0` image. Guaranteed correct output on
    any ESC/POS printer for any Unicode input (cyrillic, ї/є/і/ґ,
    emoji). ~20–50 KB per receipt on the wire.
  - `"native"` — printer's built-in font with `code_page` selecting the
    table. Faster + thinner stroke, but the firmware must actually have
    the configured page. Run `/devices/{id}/probe-codepage` first to
    pick the right one.
- `code_page` (only used in `native` mode):
  - `null` — let python-escpos magic-encode pick per character.
  - `"ua_cp866"` — custom encoder that extends CP866 with Ї/Є/І/Ґ at
    0xF0–0xF7 (works on most Ukrainian POS printers).
  - `"cp866"` / `"cp1251"` / `"cp1125"` / ... — hand to magic_encode.
- `drawer_pin`: `0` / `1` per the wiring; `null` disables the drawer
  endpoint for that printer.

## Frontend settings flow

```
 Settings → Принтери
 ─────────────────────────────────────────────
 [+ Виявити пристрої]   POST /devices/discover
 ─────────────────────────────────────────────
   ▢ STMicro USB POS Printer  (f81ba96ad564)
       Роль:   [Чеки ▼]
       Імʼя:   [Бар-чек          ]
       Папір:  ( ) 58мм   (•) 80мм
       Режим:  (•) Bitmap  ( ) Native
       [Тест друку]   POST /devices/{id}/test-print
       [Зберегти]     POST /devices/register
```

Bitmap is the default — UX-wise the operator can just press Save and
move on. The Native option exists for power users who want native-font
output and accept that they may need to run `/probe-codepage` to find a
table their hardware understands.

## Phase 2 transports (already scaffolded)

`src/devices/scan.py` exposes `discover_network()` and `discover_bluetooth()`
that currently return empty lists; `POST /devices/discover` aggregates
them. Plan:

- **Network** — mDNS Bonjour browse for `_pdl-datastream._tcp` and
  `_ipp._tcp`, plus a fallback nmap-style sweep of port 9100 over a CIDR
  the user supplies in settings.
- **Bluetooth** — list paired devices via `bluetoothctl` (Linux) or
  `pybluez` (cross-platform), filter on UUIDs/class advertised by ESC/POS
  thermal printers. Connect over RFCOMM channel 1.

In both cases the descriptor + id model is unchanged — only the transport
field and the address sub-object differ.

## BarHandler-side stories (proposed BH-*)

### Epic: "Local printer integration (barhandler-manager)"

#### Story 1 — Manager URL + API key in Settings → Інтеграції
Add a new "Принтери" subsection alongside the existing fiscal-integration
list with:
- `managerUrl` (default `http://localhost:9999`)
- `managerApiKey`
- "Тест підключення" button → `GET /health`, surfaces the per-printer
  status badges on success.

#### Story 2 — "Виявити пристрої" flow
- Button calls `POST /devices/discover`.
- Renders a list of cards: label / transport / vendor:product.
- Per card the user picks role (`Чеки` / `Кухня`) + optional nickname +
  paper width + code_page (default Auto).
- "Зберегти" → `POST /devices/register`. App settings store the returned
  `id` keyed by role (`receiptPrinterId`, `kitchenPrinterId`).

#### Story 3 — Fiscal print effect
- New NgRx action `PrintFiscalReceipt` triggered after
  `CheckboxGetReceiptSucceeded` / `VchasnoKasaGetReceiptSucceeded`.
- Mapper builds the `FiscalReceipt` body (numeric fields, not the
  pre-formatted strings already in `IReceiptModel`) — see field-mapping
  table below.
- POST to `${managerUrl}/print/fiscal?printer_id=${receiptPrinterId}`.
- 503 → "Принтер недоступний" toast.
- Network error → fall back to the existing browser-print path.

#### Story 4 — Kitchen ticket
- When an order is created with `Їжа` items, additionally POST to
  `/print/kitchen?printer_id=${kitchenPrinterId}` with `{order_number,
  table, guest, items: [{name, qty, note}]}`.

#### Story 5 — Checkbox text shortcut (optional perf path)
- Instead of building the `FiscalReceipt` payload for Checkbox, the
  frontend may call
  `GET https://api.checkbox.in.ua/api/v1/receipts/{id}/text?width=32`
  and POST the body to `/print/text` verbatim. Already legal-compliant
  per Наказ №329 від 08.06.2021.

#### Story 6 — Drawer toggle on payment
- Optional checkbox on payment modal "Відкрити касовий ящик". When set,
  include `"open_drawer": true` in the print payload.

#### Story 7 — Manager health pill
- Header status pill alongside existing fiscal POS pills — shows
  manager + each registered printer. Poll `GET /health` on login + every
  30 min.

## IReceiptModel → FiscalReceipt mapping

| FiscalReceipt field | Source today |
|---|---|
| `business_name` | from fiscal-operator settings (ФОП name) |
| `point_name` / `address` | from fiscal-operator settings |
| `tax_id` | Checkbox `transaction.tax_id` / Vchasno `company_edrpou` |
| `establishment` | current `Settings → Інтеграції → Заголовок чеку` |
| `items[].name` | `IReceiptModel.products[].name` |
| `items[].quantity` | numeric from raw response (currently buried in `quantityAndPrice`) |
| `items[].price` | numeric (Checkbox `good.price/100`, Vchasno `price`) |
| `items[].sum` | numeric (Checkbox `sum/100`, Vchasno `sum`) |
| `items[].tax_symbol` | currently last char of `priceAndTax` — keep numeric flag instead |
| `items[].uktzed` / `barcode` | already there |
| `items[].excise_codes` | split `exciseValue` into an array |
| `payment_name`, `operation` | already there |
| `paid_sum` / `total_sum` | numeric variants of current strings |
| `taxes[]` | split current `taxName` string into `{name, symbol, rate}` |
| `acquirer.*` | already there in `payment.*` (Vchasno only) |
| `fiscal_number`, `fiscal_date`, `pos_fiscal_number`, `cashier` | already there |
| `qr_url` | `qrCodeUrl` |
| `operator` | from `fiscalOperatorName` enum |
| `footer` | `receiptFooter` from settings |

## Out of scope (Phase 2+)

- LAN + Bluetooth printer discovery (stubs already present)
- Monobank acquiring through the manager
- Label printer (TSPL / ZPL)
- Web UI inside the manager — frontend lives in BarHandler / FitStudio
