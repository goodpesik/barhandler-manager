# BarHandler ↔ Barhandler Manager integration spec

How the BarHandler web app (Angular) drives `barhandler-manager` for receipt
printing. **Status:** scoped, NOT implemented in BarHandler yet — we land
this after Phase 1 of the manager is working with a real printer.

## TL;DR

When a transaction is recorded, BarHandler:

1. Asks the fiscal operator (Checkbox / Vchasno Kasa) for the receipt data
   it already does (`facade.CheckboxGetReceipt` / `facade.vchasnoKasaGetReceipt`).
2. Calls a NEW mapper `fiscalReceiptToManagerPayload(...)` that flattens
   either Checkbox or Vchasno response into the unified `FiscalReceipt`
   shape this manager understands (see `src/models/fiscal_receipt.py`).
3. POSTs to `http://localhost:9999/print/fiscal` with the
   `X-Api-Key` configured in settings.

For Checkbox the **simpler** alternative is forwarding the pre-rendered text
to `/print/text` — that endpoint is regulation-compliant out of the box
(Naказ №329 від 08.06.2021).

---

## Endpoints to hit

| Endpoint | When |
|---|---|
| `POST /print/receipt` | Internal/non-fiscal receipts (no integration enabled) |
| `POST /print/fiscal`  | Fiscal receipts (Checkbox OR Vchasno Kasa) — preferred, single visual style |
| `POST /print/text`    | Checkbox shortcut — forward `GET /api/v1/receipts/{id}/text?width=<N>` body verbatim |
| `POST /drawer/open`   | Manual cash-drawer pulse |

Auth: `X-Api-Key: <key>` (same key from `barhandler-manager/config.yaml`).

---

## Stories for Jira (proposed BH-* tickets)

### Epic: "Local printer integration (barhandler-manager)"

#### Story 1 — Manager URL + API key in Settings
- Add fields `managerUrl` (default `http://localhost:9999`) and `managerApiKey`
  to the existing General settings section.
- Add a "Тест підключення" button that hits `GET <url>/health` (no auth) and
  reports the `devices.receipt` status (connected / unavailable / not_configured).

#### Story 2 — Fiscal receipt mapper
- New file `src/app/helpers/manager-payload.helpers.ts`.
- Function `toFiscalReceiptPayload(receiptData: IReceiptModel, langService): FiscalReceiptPayload`.
  - Convert pre-formatted strings in `IReceiptModel` back into numeric fields
    OR refactor the Checkbox / Vchasno mappers to retain numeric values
    alongside the existing string columns (recommended).
  - Fill the unified payload (see `docs/samples/fiscal-receipt-vchasno.json`
    and `fiscal-receipt-checkbox.json` for the shape).

#### Story 3 — Print effect
- New NgRx action `PrintFiscalReceipt` triggered after
  `CheckboxGetReceiptSucceeded` / `VchasnoKasaGetReceiptSucceeded`.
- Effect: build payload via mapper, POST to `${managerUrl}/print/fiscal`.
- On 503 → "Принтер недоступний" toast. On network error → fall back to the
  existing browser-print path (so prints still work without the manager).

#### Story 4 — Drawer toggle on payment
- Optional checkbox on payment modal "Відкрити касовий ящик" — when set,
  include `"open_drawer": true` in the print payload.

#### Story 5 — Manager health polling
- Add a status pill to the header (alongside the existing fiscal POS pills)
  that reflects manager + receipt printer status.
- Poll `GET /health` on login + every 30 min (matches manager design).

---

## Field mapping reference

`IReceiptModel` already has both numeric and string-y fields after the
current mappers run. To produce a `FiscalReceipt` payload, the new mapper
needs to:

| FiscalReceipt field           | Source today |
|-------------------------------|--------------|
| `business_name`               | (new) — pull from settings or fiscal operator response |
| `point_name`                  | (new) — from operator settings |
| `address`                     | (new) — from operator settings |
| `tax_id`                      | Checkbox: `transaction.tax_id` / Vchasno: `company_edrpou` |
| `establishment`               | `receiptHeader` (current `Settings → Інтеграції → Заголовок чеку`) |
| `items[].name`                | already in `IReceiptModel.products[].name` |
| `items[].quantity`            | currently in `quantityAndPrice` ("1 x 263.00") — need numeric `qnt` |
| `items[].price`               | numeric (Checkbox `good.price/100`, Vchasno `price`) |
| `items[].sum`                 | numeric (Checkbox `sum/100`, Vchasno `sum`) |
| `items[].tax_symbol`          | last char of `priceAndTax` — store numerically |
| `items[].uktzed` / `barcode`  | already present |
| `items[].excise_codes`        | `exciseValue` split — keep as array |
| `payment_name`                | already present |
| `operation`                   | `payment.operation` (Vchasno) / derived for Checkbox (`Оплата`/`Повернення`) |
| `paid_sum` / `total_sum`      | numeric variants of current strings |
| `taxes[].name`/`.symbol`/`.rate`/`.value` | currently `taxName` string — split into parts |
| `acquirer.*`                  | already present in `payment.*` (Vchasno only) |
| `fiscal_number`               | already present |
| `fiscal_date`                 | already present (ISO) |
| `pos_fiscal_number`           | already present |
| `cashier`                     | already present |
| `qr_url`                      | already present (`qrCodeUrl`) |
| `operator`                    | from `fiscalOperatorName` enum (`'checkbox'` / `'vchasno_kasa'`) |
| `footer`                      | `receiptFooter` from settings |

---

## Out of scope (Phase 2)

- Monobank acquiring through the manager (currently the web app talks to
  Monobank directly; manager exposes a stub at `/terminal/charge`).
- Label printer (TSPL/ZPL).
- Multiple printers per venue.
