# Architecture

## What the manager does

A local HTTP service that sits between the operator's web POS (`bar-handler-app`) and the physical peripherals attached to the operator's machine:

- USB / Bluetooth / network thermal printers (receipt + label, ESC/POS or TSPL)
- POS terminals for card payments (Mono / Privat / Raif / Pivdenny — all speak the SSI ECR JSON protocol over TCP)

The browser can't talk to USB/Bluetooth/TCP directly. The manager exposes a JSON API on `http://localhost:9999` that the browser hits, and translates each call into the appropriate hardware operation.

## Repo layout

```
barhandler-manager/
├── main.py                     # Entry point — loads config, calls server.create_app()
├── cli.py                      # Operator CLI (start/stop/status/restart/logs)
├── config.yaml                 # Operator config (X-Api-Key, optional CORS overrides)
├── VERSION                     # Bumped by publish CI on every production release
├── requirements.txt
├── installers/
│   ├── install.sh              # macOS + Linux installer (Homebrew/apt)
│   ├── install-android.sh      # Termux installer
│   └── install.ps1             # Windows (field-untested)
├── scripts/
│   └── usb_probe.py            # Standalone USB diagnostic
├── src/
│   ├── server.py               # FastAPI app, middleware (CORS+PNA), routes wiring
│   ├── config.py               # YAML loader, install-root aware
│   ├── devices/
│   │   ├── registry.py         # Printer registry (persists to printers.json)
│   │   ├── printer.py          # Device wrapper, status, async job queue
│   │   ├── terminal_registry.py
│   │   └── scan.py             # Discover USB / network / BT
│   ├── models/
│   │   ├── printer.py          # PrinterDescriptor, PrinterRegistration, PrintProtocol
│   │   ├── terminal.py         # ChargeRequest, AcquirerResult, MerchantBinding
│   │   ├── receipt.py
│   │   └── fiscal_receipt.py
│   ├── routes/
│   │   ├── health.py           # GET /health (status, version, printers summary)
│   │   ├── devices.py          # /devices/* — discover, register, probe-codepage
│   │   ├── terminal.py         # /terminal/* — discover, register, charge, cancel
│   │   ├── print_routes.py     # /print/* — receipt, fiscal, label, kitchen, lines, text
│   │   ├── drawer.py           # /drawer/open
│   │   ├── system.py           # /system/* — update, logs, usb-probe
│   │   └── dashboard.py        # GET / — operator-facing HTML dashboard
│   └── services/
│       ├── bitmap_render.py    # PIL → ESC/POS raster, paper-width-aware
│       ├── tspl_render.py      # PIL → TSPL bitmap framing
│       ├── encoding.py         # Cyrillic codepage helpers for ESC/POS
│       ├── fiscal_receipt.py   # FiscalReceipt → printer ops
│       └── terminals/
│           ├── base.py         # TerminalAdapter ABC + TerminalUnavailable
│           ├── ssi.py          # SSI ECR JSON adapter (Mono / Raif / Pivdenny)
│           └── privatbank.py   # PrivatBank PB-JSON adapter
├── docs/
│   ├── SSI-ECR-JSON-protocol-v1.3.2.pdf   # Authoritative spec
│   ├── INTEGRATION-SPEC.md
│   └── agent-context/          # ← you are here
└── tests/                      # pytest, CI gates PRs on these
```

## Release flow

- Branch model: `main` (dev) → `production` (releases).
- CI on push to `production` (`.github/workflows/publish.yml`): auto-bumps VERSION + creates GitHub release with tag `v0.3.X`.
- Installer tarball-URL: `https://github.com/goodpesik/barhandler-manager/archive/refs/heads/production.tar.gz` (HEAD of production, NOT a tag). Means rollback = force-push, no easy revert.
- Latest release: `https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.sh` (+ `install-android.sh`).
- Operator updates: dashboard "Оновити" button → `POST /system/update` → spawns `update.sh` → curls installer with `--force`. Manager restarts itself.

## SSI ECR JSON protocol

Wire-level brief; full spec in `docs/SSI-ECR-JSON-protocol-v1.3.2.pdf`.

### Transport

- TCP socket on port 3000 (HTTP variant on 3001, we use TCP — universal across firmware versions).
- Frame: `<STX 02 66 01> <LEN 2B big-endian> <DATA UTF-8 JSON ≤64K> <LRC 1B>`, LRC = XOR of every DATA byte.
- 15-second per-request timeout; ≥0.25s pause between consecutive requests (protocol minimum, §1.1).
- No auth at the wire layer — terminal trusts everyone on the LAN.

### Purchase flow (§3.1, §5.2.1)

**Critical:** Mono SSI is a TWO-STEP transaction. Single-step PRs were declining cards in the field for an entire afternoon because step:2 was missing — see [troubleshooting.md → "Card declined when paid via SSI but works on manual entry"](./troubleshooting.md#card-declined-when-paid-via-ssi-but-works-on-manual-entry).

1. ECR sends `{"method":"Purchase", "step":"1", "params":{transAmount, transCurrency, merchantId, ...}}`.
   - `transAmount` is **kopecks** as a **string**: `"100"` = 1 UAH.
   - `transCurrency` is the ISO 4217 code as a string: `"980"` = UAH.
   - `merchantId` is the SSI merchant identifier (12-15 digits).
   - Optional: `transactionUid` (our external ID, echoed back), `discountedAmount`, `splitData`.
2. Terminal acks `{error: false}` immediately, then enters busy state (status S01→S02→S03→S04→S05).
3. ECR polls `{"method":"GetStatus"}` every 500ms (with 300ms inter-request pause). Wait for `status` to become **S00** (idle, single-step done) OR **S08** (waiting for step 2).
4. ECR calls `GetLastResult` (or `GetResultByUid` if we have a `transactionUid`).
   - If `transactionResult` = `"FIRST_STEP_COMPLETED"` OR the idle status was S08 → **send Purchase step:2 with same params**, wait for next S00, call GetLastResult again. The second call has the real `APPROVED`/`DECLINED` + `rrn` + `authCode`.
   - If `transactionResult` is anything else → that's the final outcome. Map via `_OK_RESULTS` / `_CANCELLED_RESULTS` sets in `ssi.py`.
5. On `status == "ok"` (success), pull `{"method":"GetLastReceipt"}` — terminal hands back the cardholder slip text (plain text on Linux X990, JSON array with `DynamicImageItem` hex-PNG on Android). Stored in `AcquirerResult.terminal_receipt` so the frontend can print it on a receipt printer. (Terminal doesn't auto-print slip in SSI-driven mode by design — the ECR owns slip printing.)

### Status codes (§4.3.4)

| Code | Meaning |
|---|---|
| S00 | Idle (operation complete OR no operation) |
| S01 | Busy, executing transaction |
| S02 | Waiting for card insert |
| S03 | Waiting for PIN / signature |
| S04 | Talking to bank |
| S05 | Printing receipt |
| S06 | Needs Z-report |
| S07 | Cardholder must remove card |
| S08 | Waiting for step:2 of multi-pass operation |

### Error codes (§4.3.3)

**Two distinct categories — handle differently.**

**"Запит" (Request, E00–E09)** — protocol/format problems. These are SERVICE failures, raise `TerminalUnavailable` → HTTP 503 to the frontend.

| Code | Meaning |
|---|---|
| E01 | Protocol version not supported |
| E02 | Checksum error |
| E03 | JSON format error |
| E04 | Required fields missing |
| E05 | Unknown method |
| E06 | Terminal busy |
| E07 | Merchant ID not found |
| E18 | Printer out of paper / printer inoperational |
| E19 | Transaction amount out of limit |

**"Операція" (Operation, E10–E22, mostly)** — legitimate transaction outcomes. These should surface as `AcquirerResult{status:"declined"|"cancelled"}` with HTTP 200, not 503. Implemented in `_business_error_to_result()` in `ssi.py`:

| Code | Maps to | Meaning |
|---|---|---|
| E10 | declined | Connection error (bank link down) |
| E11 | declined | Verification error (PIN/signature) |
| E12 | **cancelled** | Operator/cardholder hit Cancel |
| E16 | declined | Card read error |
| E17 | declined | EMV error |

### `step` field

Generally `Optional` (§4.2.1 global key table), but **mandatory** for Purchase (§5.2.1.1 — bold in example = required). The reference emulator (`SSI Json Emul 2`) doesn't send `step` because it predates spec v1.2.6 (2025-01-31) which formalised it. **Trust the PDF, not the emulator** when they disagree.

## Frontend integration shape

The manager response on `POST /terminal/charge`:

```json
{
  "terminal_id": "651fc4b1dbac",
  "result": {
    "status": "ok",          // or "declined" / "cancelled"
    "rrn": "024106595666",
    "auth_code": "072934",
    "cardmask": "559532419******4",
    "paysys": "MASTERCARD",
    "bank_name": "АТ Універсал банк",
    "terminal_id": "PQ016684",
    "pos_entry_mode": "CONTACTLESS",
    "invoice_num": "000332",
    "raw_transaction_result": "APPROVED_ONLINE",
    "error_code": null,
    "error_message": null,
    "terminal_receipt": "<text or JSON-array of DynamicImageItem>",
    "vendor_data": { ...full GetLastResult.params verbatim... }
  }
}
```

Frontend should `switch` on `result.status`:
- `"ok"` → `CreditCardPaymentStatus.Approved`
- `"cancelled"` → `CreditCardPaymentStatus.Canceled` (Vchasno Kasa enum, reused)
- `"declined"` → `CreditCardPaymentStatus.Declined`
- everything else → `CreditCardPaymentStatus.Error`

## CORS + PNA

`src/server.py` has TWO middlewares:
1. `CORSMiddleware` (Starlette) — gates on `allow_origins` + `allow_origin_regex`. Default regex covers Firebase Hosting (`*.web.app`/`*.firebaseapp.com`), per-tenant subdomains of `barhandler.com`/`petshandler.com`/`fitstudiocrm.com`, `localhost` (any port, any scheme including `capacitor://`).
2. `cors_and_pna` (ours, `@app.middleware("http")`) — short-circuits Chrome 117+ Private Network Access preflight (`Access-Control-Request-Private-Network: true`). Starlette returns 400 on these by default with no opt-in; we answer them directly with `Access-Control-Allow-Private-Network: true` plus the standard CORS echo headers, gated on the same origin allow-list. See [troubleshooting.md → "PWA can't reach localhost manager"](./troubleshooting.md#pwa-cant-reach-localhost-manager).

`allow_credentials=False` — we auth via custom `X-Api-Key` header, not cookies, so credentials gain nothing and break stricter clients (Android Chrome) when combined with `allow_headers=["*"]`.
