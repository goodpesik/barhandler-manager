# Troubleshooting

Diagnostic flowcharts for the recurring "doesn't work" tickets. Each entry has the symptom, the confirming test, and the fix that made it permanent.

## Card declined when paid via SSI but works on manual entry

**Symptom:** Operator hits Pay on the POS frontend → terminal shows the amount → cardholder taps card → "Картку відхилено". Same card on the same terminal with **manual amount entry** works fine.

**Confirming test:** Look at `bhm.log` for the charge attempt. You'll see something like:
```
→ Purchase step:1
← {error: false}
→ GetStatus
← {status: "S08", busyCause: "Очікується другий крок поточної операції"}
→ GetLastResult
← {transactionResult: "FIRST_STEP_COMPLETED", pan: "...", rrn: "", authCode: ""}
charge final: status=declined raw=FIRST_STEP_COMPLETED
```

**Root cause:** Mono SSI is a TWO-STEP Purchase. After step:1 the terminal parks in S08, returns `FIRST_STEP_COMPLETED` as a placeholder. ECR MUST then send `Purchase step:2` (same params) to trigger the actual bank authorisation. Without step:2 the bank never sees an auth request; manager returns the placeholder mapped to "declined" since it's not in `_OK_RESULTS` / `_CANCELLED_RESULTS`.

**Fix:** Implemented in v0.3.18 — `_wait_idle()` now returns the status that broke its loop; `charge()` checks for S08 or `FIRST_STEP_COMPLETED` and sends `Purchase step:2` before the final `GetLastResult`. See `src/services/terminals/ssi.py charge()`.

**Where the spec said so:** PDF p.17 flowchart explicitly has the "потребує виконання 2 кроку?" diamond. Diamond was on the chart, code wasn't behind it.

---

## Terminal slip doesn't print after successful charge

**Symptom:** Card payment via SSI succeeds (status: "ok", RRN, auth code), but the terminal doesn't print its cardholder receipt. Manual-mode payment on the same terminal does print.

**Root cause:** In SSI-driven mode the terminal **deliberately doesn't print** the slip — by spec, the ECR (cash register) owns slip printing. Mono returns the slip text via `GetLastReceipt` (§5.5.2) and expects the ECR to print it on its own receipt printer.

There's no SSI method that tells the terminal "print your slip now" — verified by reading the full PDF and checking the reference emulator (`SSI Json Emul 2`) which only has `GetLastReceipt` / `GetLastReportReceipt`, no `PrintReceipt` of any kind.

**Fix:** v0.3.19 calls `GetLastReceipt` after successful step:2 and stashes the body in `AcquirerResult.terminal_receipt`. Frontend can route this onto the receipt printer.

**Format gotcha:** plain text on Linux X990 terminals, but **JSON array of `DynamicImageItem` with hex-encoded PNG** on Android-based terminals (Mono's mobile-style devices). Need a small renderer that:
1. Parses the JSON
2. Decodes each `DynamicImageItem.data` hex string into bytes → PIL Image
3. Vstacks images into a single bitmap sized for the printer's paper width
4. Sends via ESC/POS raster (`bitmap_render.image_to_gs_v_0`)

Not yet implemented. Listed in [open-tasks.md](./open-tasks.md).

---

## PWA can't reach localhost manager

**Symptom:** PWA at `https://something.barhandler.com` (or other tenant subdomain) shows "Не вдалося зв'язатися з менеджером". DevTools Network has `health preflight 400` followed by `health xhr CORS error`. DevTools Issues panel says "HTTP status of preflight request didn't indicate success".

### Tests in order (each takes 30 seconds, each rules things out)

**Test 1 — is HTTP localhost accessible at all from the browser?**
Open `http://localhost:9999/health` directly in a new tab. If you see `{"status":"ok",...}` → localhost is fine, mixed content isn't blocking. If `ERR_CONNECTION_REFUSED` → manager isn't running on the device the browser is on (Android emulator localhost ≠ host machine localhost — `10.0.2.2` is host on AVD).

**Test 2 — does the manager itself answer preflight correctly?**
On the device running the manager:
```bash
curl -i -X OPTIONS http://localhost:9999/health \
  -H "Origin: https://your-tenant.barhandler.com" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: x-api-key"
```
Should be `HTTP/1.1 200 OK` with `access-control-allow-origin` echoing your origin. If 400 → see "What broke CORS" subsections below.

**Test 3 — is it actually Private Network Access blocking?**
Same curl but **add** `-H "Access-Control-Request-Private-Network: true"`. If THIS one returns 400 "Disallowed CORS private-network" while test 2 returned 200 → it's PNA. (Pre-v0.3.27 this was the case; post-v0.3.27 should be 200 with `access-control-allow-private-network: true`.)

### Three distinct underlying causes

#### 1. PNA preflight rejection (Chrome 117+)

**Confirming test:** Test 3 above returns 400 "Disallowed CORS private-network".

**Cause:** Chrome 117+ enforces Private Network Access — fetches from a "public" origin (https://*.web.app or https://*.barhandler.com) to a "private" target (anything in `127/8`) need an additional preflight signal: client sends `Access-Control-Request-Private-Network: true`, server MUST echo `Access-Control-Allow-Private-Network: true`. Starlette's CORSMiddleware refuses these preflights by default with no opt-in.

**Fix:** v0.3.27 — our `cors_and_pna` middleware short-circuits PNA preflights before they hit Starlette. See `src/server.py`.

**Operator-side workaround:** `chrome://flags/#block-insecure-private-network-requests` → Disabled. Temporary, for testing.

#### 2. CORS spec conflict: `allow_credentials=True` + `allow_headers=["*"]`

**Confirming test:** Test 2 returns 400 even with no PNA header; the request shows `Access-Control-Request-Headers: x-api-key` and the response has no `access-control-allow-headers`.

**Cause:** CORS spec forbids wildcard `Access-Control-Allow-Headers` when credentials are in play. Starlette refuses to send the wildcard with credentials enabled, returning 400 to stricter clients (Android Chrome). Desktop Chrome was more lenient and let it through, hiding the bug.

**Fix:** v0.3.26 — `allow_credentials=False`. We auth via `X-Api-Key` (custom header, not cookies); browsers don't ship custom headers automatically, so credentials=True was buying nothing while breaking stricter clients.

#### 3. Origin not in allow-list

**Confirming test:** Look at `bhm.log` after a failed preflight. If there's `preflight REJECTED origin=https://X path=/devices (not in allow_origins and doesn't match regex)` — the origin isn't covered.

**Fix:** Add to `cors_origin_regex` in `src/server.py`. Current regex covers:
- Any Firebase Hosting subdomain (`*.web.app` / `*.firebaseapp.com`)
- `*.barhandler.com` / `*.petshandler.com` / `*.fitstudiocrm.com` (apex + any subdomain depth)
- `http(s)?://localhost(:port)?` + `capacitor://localhost`

If a new tenant gets a different apex domain, add an analogous regex branch.

### Browser-side caching gotchas

After fixing a CORS issue server-side, the client browser may still show the failure for a while due to:
- **Preflight cache:** up to 2 hours by default (`Access-Control-Max-Age: 600` from manager). Hard refresh (Cmd+Shift+R) usually clears.
- **Service Worker cache:** Angular's `ngsw-worker.js` intercepts fetch and may serve old responses. DevTools → Application → Service Workers → Unregister, then Clear site data.
- **HSTS/HTTP cache:** Chrome may have decided the site is HTTPS-only. `chrome://net-internals/#hsts` if needed.

---

## Manager doesn't find network terminal even though same Wi-Fi

**Symptom:** Operator says "I plugged the terminal into the same Wi-Fi as the tablet/Mac, but Discover Terminals returns empty." Or returns terminals from a different network than expected.

### Diagnostic flow

**Look at `bhm.log`** after pressing Discover. As of v0.3.28 the manager logs:
```
terminal discovery: scanning subnet 192.168.X.0/24 (254 hosts) ports SSI=3000 PB=2000 tcp_timeout=0.3s
terminal discovery: TCP-open hosts SSI=['192.168.X.Y'] PB=[]
```

Or on failure:
```
terminal discovery: TCP-open hosts SSI=[] PB=[]
terminal discovery: nothing answered on port 3000 (SSI) or 2000 (PB) across 192.168.X.0/24...
```

### Common causes by probability

**1. AP isolation / Client isolation** — most consumer routers (and especially guest networks) enable this by default. Devices on the same SSID can't talk to each other directly, only to the router. Disable in router admin: Wi-Fi Settings → "AP Isolation" / "Client Isolation" / "Wireless Isolation".

**2. Different subnets / VLAN** — operator's "main" and "guest" Wi-Fi networks are separate subnets (e.g., 192.168.0.0/24 vs 192.168.50.0/24). Manager's host is on one, terminal on the other. Confirm: check IPs on both devices, first three octets should match.

**3. Tablet hotspot scenario — multi-interface trap.** Tablet shares its mobile data as a Wi-Fi hotspot, terminal connects to it. Tablet has TWO IPv4 addresses:
- `192.168.43.1` (hotspot AP, where terminal lives)
- `10.X.X.X` (cellular, primary outgoing)

`_local_subnet()` uses the UDP-connect-to-8.8.8.8 trick to pick the primary outgoing interface — that returns the CELLULAR IP, not the hotspot. Scanning the cellular subnet finds nothing because the terminal is on the hotspot.

**Status:** known limitation. Listed in [open-tasks.md](./open-tasks.md) as "rewrite `_local_subnet()` to enumerate ALL local IPv4 interfaces, scan each /24".

**4. Sleep / Doze on Android** — battery saver suppresses background network activity. Less common but worth checking on Android hosts.

**5. Terminal on non-default port** — manager scans TCP/3000 (SSI) and TCP/2000 (PB). If the terminal firmware was configured for a custom port, won't be found. Verify via terminal's own admin menu.

**Quick manual verification** from the manager's host:
```bash
curl -v --max-time 3 telnet://<terminal-ip>:3000
# OR
nc -zv <terminal-ip> 3000
```
If this connects, the terminal is reachable; discovery's failing on the protocol probe. If it doesn't connect, it's a network/firewall issue.

---

## Dashboard "Оновити" button does nothing

**Symptom:** Operator hits the Update button, button shows "Перезапуск…", nothing happens. Health version stays at the old value.

### History of root causes (don't repeat any of these)

**Pre-v0.3.7:** Popen wrote stdout/stderr to `/dev/null`. Whatever the installer was failing on was invisible. **Fix:** route to `~/.barhandler-manager/update.log` with a timestamped header per attempt.

**Pre-v0.3.12 (macOS):** install.sh died at "Homebrew not installed" because launchd-spawned subprocess inherited bare PATH without `/opt/homebrew/bin`. **Fix:** three-layer PATH bootstrap (Popen env in `system.py` + `export PATH` in generated `update.sh` + `export PATH` + `find_brew()` in `install.sh`).

**Pre-v0.3.16 (both Mac and Termux):** `pkill` sent SIGTERM, uvicorn graceful shutdown took 5+ seconds, our `sleep 1` after pkill was too short. Old process kept holding port 9999 while new launchd-spawned process spent the next 30 minutes failing to bind. Post-bootstrap health poll just checked "did /health respond" without checking WHICH version, so a dying old process answering for a few seconds tricked install.sh into declaring success. **Fix:** post-pkill pgrep loop waits up to 10s, then SIGKILL stragglers; health poll reads the `VERSION` file and only declares success when `health.version` matches it.

**Pre-v0.3.20 (macOS Intel):** `python3 -m venv` used Apple CLT's Python 3.8. brew installed python@3.11 but the venv was never recreated. pydantic v2 crashed on `list[X]` PEP-585 syntax. **Fix:** `find_python()` probes versioned binaries (python3.13/3.12/3.11) explicitly; venv recreated if `.venv/bin/python --version` < 3.11.

**Pre-v0.3.23 (Termux):** Termux upgraded default python 3.11→3.13. No pydantic-core wheel for android_arm64+3.13 → maturin compile from source → `Failed to determine Android API level`. **Fix:** export `ANDROID_API_LEVEL=24` before pip install; recreate venv when system python version no longer matches venv's.

### Current diagnostic (v0.3.22+)

Dashboard has a 📋 Logs button with tabs for `bhm.log`, `bhm.boot.log`, `update.log`. Operator clicks `update.log` to see what happened on the last attempt. No more SSH'ing required.

If update.log says "manager up at http://localhost:9999 (took 0s)" — old process never died, see the "Pre-v0.3.16" entry above. Should not happen on v0.3.16+ but worth checking.

---

## "ModuleNotFoundError: No module named 'uvicorn'"

**Symptom:** Manager fails to start. `bhm.boot.log` shows `ModuleNotFoundError: No module named 'uvicorn'`.

**Cause:** pip install bailed mid-way and uvicorn was never installed. Usually because pydantic-core compile failed earlier (see Termux Python 3.13 + maturin entry above).

**Fix:** Rerun `--force install`. On v0.3.23+ this also recreates the venv if Python version changed.

---

## "address already in use" on port 9999

**Symptom:** `bhm.boot.log` ends with `[Errno 48] error while attempting to bind on address ('127.0.0.1', 9999): address already in use`.

**Cause:** Old manager process still holding the port. Either pkill missed it (process ran under different cmdline pattern) or launchd's `KeepAlive=true` respawned it between our pkill and the new bootstrap.

**Manual cleanup:**
```bash
# macOS:
launchctl bootout gui/$(id -u)/com.goodpesik.barhandler-manager 2>/dev/null
pkill -9 -f "$HOME/.barhandler-manager/main.py"
sleep 2
lsof -i :9999    # should be empty
curl -fsSL https://github.com/goodpesik/barhandler-manager/releases/latest/download/install.sh | bash -s -- --force

# Termux (lsof -i unsupported; alternatives):
pgrep -af main.py
cat /proc/net/tcp | awk '$2 ~ /:270F$/' | head -3   # 9999=0x270F
sv down barhandler-manager 2>/dev/null
pkill -9 -f "$HOME/.barhandler-manager/main.py"
```

---

## Quick reference: log files

| Path | What's in it |
|---|---|
| `~/.barhandler-manager/bhm.log` | Python logger output: SSI flow, charges, CORS rejections, terminal discovery progress. Rotated 5×5MB. |
| `~/.barhandler-manager/bhm.boot.log` | uvicorn stdout/stderr from nohup-spawned process. Startup tracebacks, port-bind errors. |
| `~/.barhandler-manager/update.log` | Stdout/stderr from each dashboard-triggered update attempt, timestamped header per attempt. |
| `~/.barhandler-manager/log/current` | runit svlogd output (Termux only). |

All three are accessible from the dashboard via the 📋 Logs button (v0.3.22+).
