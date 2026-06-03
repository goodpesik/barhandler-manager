# Session log

Chronological account of every fix, with the bug each one solved. Useful when investigating regressions or wondering "why on earth does the code do X."

Format: `vX.Y.Z` — `commit-hash` — one-line summary. Detail underneath.

---

## 2026-06-01 (marathon day 1)

Continued from a prior context-compacted session. Started mid-debug of the payment-modal terminal flow, ended with v0.3.15 + 7 frontend commits + Android-network-only decision + SSI business-error mapping.

### v0.3.7 — README rewrite + earlier fixes (carried over from prior session)

Reference point. Pre-existing baseline before the marathon.

### v0.3.8 — `2344ac6` — `ManagerPrinterNotFound` distinct from 503

When the manager returns 404 on a stale printer ID, frontend was showing "Менеджер недоступний" (implying a connection problem) instead of "Принтер не знайдений". `_BarHandlerManagerService.unreachable` interceptor now tags 404 with `printerNotFound: true`; new translation key `ManagerPrinterNotFound` differentiates from real connection failures.

### v0.3.9 — `5e78169` — actually charge manager terminal on card payment (was no-op)

The original critical fix: `chargeManagerTerminal()` in payment-modal was **dead code** — payment modal stored some IDs and closed, never calling `chargeTerminal()`. Card payment on BHM terminal silently bypassed the terminal entirely. After fix: modal stays open in Processing state, shows the spinner, awaits terminal response.

### v0.3.10 — `3c54f99` — prevent terminal bypass on race condition

Modal opens, `loadManagerTerminals()` async. If operator clicks "Credit Card" BEFORE the list returns, `hasManagerTerminals = false` → direct Pay button shows → clicking it bypasses the terminal flow. Fix: spinner during load, after-load auto-select if mode is already CreditCard.

### v0.3.11 — `55e1b37` — always fetch manager terminals (drop useDeviceManager gate)

`loadManagerTerminals()` was gated on `facade.useDeviceManager` (the global Device Manager toggle). But that toggle is about whether the manager drives the **printer** — terminals were collateral damage. If operator hadn't enabled manager printing, terminal flow couldn't even see registered terminals. Fix: always fetch terminals on modal open, fail gracefully on no manager.

### v0.3.12 — `fdd6082` — guard against undefined `devicesStatus[integrationId]`

`ngOnInit` of payment modal accessed `this.facade.devicesStatus[this.integrationId].vhasnoKasaDeviceManager` without null-checking the dict lookup. On any order without a fiscal integration set up, `devicesStatus[integrationId]` was undefined → TypeError → ngOnInit crashed → `loadManagerTerminals()` never reached. This was the actual root cause of "Зміна не відповідає" complaints in some scenarios.

### v0.3.13 — `d3f35bf` — one spinner, not two — disable Pay while terminals load

Visual cleanup of the race-condition fix. Instead of stacking a `loadingManagerTerminals` spinner on top of the existing Processing spinner, just disable the Pay button while terminals load.

### v0.3.14 — `(loader interceptor work in frontend)` — `4605c95` — cancelled/declined distinct states

Manager normalises SSI outcomes to "ok" / "declined" / "cancelled". Frontend was branching on `result.status === 'ok'` only — everything else fell into a generic Error state. Switched to a 3-way `switch` mapping `cancelled` → `CreditCardPaymentStatus.Canceled` (blue), `declined` → `Declined` (yellow), default → `Error`. Reuses the same enum the Vchasno Kasa flow uses, so no new template work.

(Frontend commit; manager release didn't change here.)

### v0.3.15 — `9a88ff5` — log every step of SSI charge flow + catch-all 500

Up to now the SSI adapter logged nothing visible at INFO. When a charge 500'd, we had no idea where. Added structured logging at every wire hop (`[ssi host:port] → Method: {...}` / `← Method: {...}`), every charge phase (`charge starting`, `charge ack OK`, `charge idle reached`, `charge final`), plus a catch-all in the `/terminal/charge` route that maps any unhandled exception onto a structured 500 (`{code:"internal_error", message:"<type>: <msg>"}`) so the frontend gets something actionable instead of a bare 500.

### v0.3.15 also — `feat(ssi): map E10-E12/E16/E17 to AcquirerResult` (`8f5617a`)

SSI spec §4.3.3 splits errorCodes into "Request" (E00-E09 — protocol issues, our problem) and "Operation" (E10-E22 — legitimate transaction outcomes). We were treating both as `TerminalUnavailable` → HTTP 503, so operator-cancel (E12) / declined card (E10/E11/E16/E17) all surfaced as "manager unavailable" instead of clean Canceled/Declined modals. New `_business_error_to_result()` helper maps these five codes to `AcquirerResult{status: declined|cancelled}` with HTTP 200. Anything else still raises.

---

## 2026-06-02 (marathon day 2)

### v0.3.16 — `786f77c` — wait-then-SIGKILL old manager + verify NEW version on /health

Field report: dashboard Update on macOS showed "manager is up (took 0s)" but health.version stayed at the old value for 30 minutes. Process list told the story: PID A (old, v0.3.12, nohup-spawned from a previous install) was still running and answering /health. PID B (new, v0.3.15, launchd-managed) had been spawned by bootstrap but couldn't bind port 9999 because the old process held it.

Two bugs compounded:
1. `pkill` SIGTERM → uvicorn graceful shutdown takes 5+ seconds, `sleep 1` was insufficient.
2. Post-bootstrap health poll only checked "did /health respond", not "with which version". Dying old process answered for the few seconds it took to drain in-flight requests, install.sh declared success.

Fix: pgrep loop waits up to 10s for actual exit, then SIGKILL stragglers. Health poll now reads `VERSION` file and matches against `health.version`.

### v0.3.17 — `7e1416e` — TSPL protocol + per-job paper size + threshold-180 binarize

User contribution (not by the agent). XP-246B / 235B / 237B ship in `Print mode: LABEL` and silently drop ESC/POS — `/print/label` returned 200 with nothing on the roll. Native TSPL support added: new `PrintProtocol` enum, label printers default to tspl, `tspl_render` module emits proper SIZE/GAP/BITMAP/PRINT framing. LabelPayload accepts width_mm/height_mm/gap_mm. bitmap_render learns 40mm=320 dots. Threshold-180 binarize replaces PIL's Floyd-Steinberg dither for crisper label text.

### v0.3.18 — `000ebf7` — send Purchase step:2 after FIRST_STEP_COMPLETED — the actual auth

THE CRITICAL FIX of the day. Field test on real Mono terminal: every card payment came back declined while manual amount entry worked fine. Logs showed:

```
→ Purchase step:1
← {error: false}
→ GetStatus
← {status: "S08", busyCause: "Очікується другий крок поточної операції"}
→ GetLastResult
← {transactionResult: "FIRST_STEP_COMPLETED", pan, rrn:"", authCode:""}
```

We treated S08 as "operation complete" and immediately called GetLastResult, which returned the placeholder. The bank never saw an auth request. `_result_from_params` correctly mapped FIRST_STEP_COMPLETED → declined given its inputs but wrong given the real terminal state.

SSI doc §3.1 (p.17 flowchart) is explicit: after the first idle, check whether the operation needs a second pass. We had the diamond on the chart, no code behind it.

Fix: `_wait_idle()` now returns the status that broke the loop; `charge()` does an interim GetLastResult right after first idle, then if `raw=FIRST_STEP_COMPLETED` OR `idle=S08`, sends `Purchase step:2` (same params), waits for next idle, fetches the real outcome.

### v0.3.19 — `3c84967` — fetch cardholder slip via GetLastReceipt after successful charge

Operator: "card payment succeeded but terminal didn't print slip — manual mode prints fine."

Confirmed by reading SSI PDF + reference emulator that there's no `PrintReceipt` method in SSI at all. In SSI-driven mode the terminal **deliberately doesn't print** the slip — by design, the ECR owns slip printing and pulls the text via `GetLastReceipt` (§5.5.2).

Added: `AcquirerResult.terminal_receipt: Optional[str]` field; after successful step:2, `charge()` calls `GetLastReceipt`, stashes the body. Best-effort: GetLastReceipt errors don't fail an already-successful charge. Format: plain text on Linux X990, JSON array with `DynamicImageItem` hex-encoded PNGs on Android terminals. Frontend wiring (parse + render + print) listed in open-tasks.

### v0.3.20 — `f3b0707` — resolve python3.11+ explicitly + recreate venv on outdated python

Yana's Mac Intel crashed at boot:
```
TypeError: Unable to evaluate type annotation 'list[MerchantBinding]'.
new typing syntax (builtins subscripting since Python 3.9) ...
/Users/Yana/.barhandler-manager/.venv/lib/python3.8/site-packages/pydantic/...
```

Two bugs: `python3 -m venv` picked Apple CLT's Python 3.8 (PEP-585 `list[X]` syntax not parseable). `brew install python@3.11` succeeded but venv was never recreated.

Fix: `find_python()` helper probes versioned binaries + Homebrew @-version prefix dirs explicitly. Venv check runs `.venv/bin/python --version` and recreates if < 3.11.

### v0.3.21 — `abe9101` — allow all Firebase Hosting subdomains via regex

Default CORS origins hardcoded `bar-handler.web.app` only. petshandler.web.app / fitstudio.web.app / preview channels all had to be added by hand. Switched to a regex covering `https://[a-zA-Z0-9-]+\.(web\.app|firebaseapp\.com)`.

### v0.3.22 — `1bd3909` — dashboard logs + USB probe + brew install libusb on macOS

Three additions:
1. `brew install libusb` on macOS install (idempotent). Without it pyusb fails fast with NoBackendError and discover_usb silently sees no printers. Linux installer always pulled it; macOS branch missed it.
2. `📋 Логи` dashboard button → tabbed panel for bhm.log / bhm.boot.log / update.log. No more SSH'ing for diagnosis.
3. `🔌 USB діагностика` dashboard button → runs `scripts/usb_probe.py` via new `POST /system/usb-probe`, shows output in the same panel.

New endpoints: `GET /system/logs?source=...&tail=N`, `POST /system/usb-probe`.

### v0.3.23 — `6b1797c` — ANDROID_API_LEVEL=24 for maturin + recreate venv on python bump

Termux upgraded default python 3.11 → 3.13 mid-2026. Client tablet fresh install failed:
```
maturin failed
  Caused by: Failed to determine Android API level.
  Please set the ANDROID_API_LEVEL environment variable.
```

PyPI has no pydantic-core wheel for android_arm64+Python 3.13, pip falls back to maturin compile which needs ANDROID_API_LEVEL. Without it install bails mid-way and uvicorn never lands → next boot `ModuleNotFoundError: No module named 'uvicorn'`.

Fix: export `ANDROID_API_LEVEL=24` + `ANDROID_PLATFORM_VERSION=24` before pip install. Recreate venv when system python version differs from venv python version.

### v0.3.24 — `b3eedf9` — log allowed origins on startup + warn on rejected preflights

Diagnostic-only. When a CORS issue appears, was it CORS / auth / mixed content / real bug? Generic 400 didn't say. Now bhm.log on startup says `CORS allowed_origins=[...] allowed_regex=...`; per-request middleware logs `preflight REJECTED origin=https://X path=/devices` when an origin doesn't match.

### v0.3.25 — `bfe6ecb` — *.barhandler.com / *.petshandler.com / *.fitstudiocrm.com subdomains

Per-tenant deployments like `biergarten-lviv.barhandler.com` were getting blocked because the default CORS list had only `https://barhandler.com` apex, and the regex only covered Firebase Hosting. Added regex branches for tenant subdomains of all three first-class operator domains.

### v0.3.26 — `bbf0ab2` — drop allow_credentials=True (spec-illegal with allow_headers=['*'])

Android Chrome inside an emulator rejected preflight as "HTTP status didn't indicate success" while curl from the same device got clean 200. Root cause: `allow_credentials=True` + `allow_headers=['*']` violates CORS spec. Starlette enforces this on stricter clients.

We auth via `X-Api-Key` (custom header, not cookies). Browsers don't ship custom headers automatically. So credentials=True was buying nothing and breaking strict clients. Flipped to False.

### v0.3.27 — `76badb2` — short-circuit Chrome PNA preflight before CORSMiddleware rejects it

THE hardest debug of the project. Even after v0.3.26 Android Chrome PWA at https://biergarten-lviv.barhandler.com still failed with the same "HTTP status didn't indicate success". curl from same device returned clean 200. Service Worker unregister, clear site data, incognito — none helped.

Final clue came from `curl -i -X OPTIONS ... -H "Access-Control-Request-Private-Network: true"`:
```
HTTP/1.1 400 Bad Request
Disallowed CORS private-network
```

Chrome 117+ enforces Private Network Access. Fetches from a "public" origin (https://...barhandler.com) to a "private" target (anything in 127/8) require an additional preflight signal: client sends `Access-Control-Request-Private-Network: true`, server MUST echo `Access-Control-Allow-Private-Network: true`. Starlette's CORSMiddleware knows about PNA but rejects-by-default with no opt-in flag.

Fix: our `cors_and_pna` middleware short-circuits PNA preflights from allowed origins, returning 200 with the full set of CORS+PNA headers, bypassing Starlette entirely. By that point CORSMiddleware-equivalent origin gating has already run in our middleware, so this isn't more permissive than the existing policy — just additive.

HTTPS on the manager wouldn't have helped — PNA enforces regardless of scheme per spec. Self-signed certs on Android need per-device trust-store install, much worse UX than one response header.

### v0.3.28 — `8c74346` — log subnet + open-port findings on terminal discovery

Operator-facing observability. "Why doesn't my manager find the terminal? we're on the same Wi-Fi." Before this, `discover_network_terminals` silently returned empty. Now bhm.log says:
```
terminal discovery: scanning subnet 192.168.0.0/24 (254 hosts) ports SSI=3000 PB=2000 tcp_timeout=0.3s
terminal discovery: TCP-open hosts SSI=['192.168.0.223'] PB=[]
```

Or on failure, an explicit warning with common-cause hints (AP isolation, different subnet, firewall).

This is where session day 2 paused. Next likely thing: `_local_subnet()` rewrite to enumerate all local IPv4 interfaces — covers the hotspot scenario where the manager host has both cellular (primary outgoing) and hotspot AP subnets, and our current code only scans the cellular one.

---

## Lessons (worth re-reading before similar projects)

1. **Don't trust exit codes you didn't write.** `launchctl` and `Starlette CORSMiddleware` both have failure modes that look like success at the API layer. Always assert on the actual end-state (process up? response 2xx? VERSION matches?).
2. **Layered debugging on physical hardware loses afternoons.** When a charge silently fails, the operator's flow has no signal. Logging at every wire hop pays for itself the first time.
3. **PDF specs > reference emulators.** The reference SSI emul (`SSI Json Emul 2`) was 1+ years behind the spec doc; trusting it would have made us strip the `step:"1"` field which was actually mandatory.
4. **Mobile Chrome is its own platform.** Desktop Chrome's leniency on CORS / mixed content / localhost / PNA does not transfer. Always test on actual mobile, with DevTools attached (`chrome://inspect`).
5. **Termux is "Linux" until it isn't.** `platform.system()` lies. Use `$PREFIX` to detect Termux explicitly, and decide per-feature what's actually supported (USB no, BT no, network yes).
6. **Service-context PATH is always bare.** Whether launchd, systemd, or runit — service-spawned subprocesses get `/usr/bin:/bin` and nothing else. Set PATH explicitly when spawning subprocesses.
7. **For SQLite + WAL: there's no easy multi-machine sync.** Operator-asked-question we punted on — addressed in `agent-context/README.md` if it comes up again, but the right answer is "use Litestream or git-the-files-while-stopped, not real-time sync."
