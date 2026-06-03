# Open tasks

Concrete things to pick up. Sorted by urgency (real-world blockers first, polish later). Update as you finish.

## High value, concrete scope

### Cardholder slip printing (frontend + manager)

**Why:** Operator's terminals don't auto-print the cardholder slip in SSI-driven mode (by design — see [troubleshooting.md → "Terminal slip doesn't print"](./troubleshooting.md#terminal-slip-doesnt-print-after-successful-charge)). Manager already fetches the slip via `GetLastReceipt` and stashes it in `AcquirerResult.terminal_receipt` (v0.3.19+). Frontend isn't wired to actually print it yet.

**Format gotcha:** Plain text on Linux X990 terminals. JSON array of `DynamicImageItem` (hex-encoded PNG) on Android-based Mono terminals. The PDF doc §5.5.2 has the format note.

**Implementation options:**

Option A — print on manager side, auto:
- New helper in `src/services/` parses the JSON slip, decodes `data` hex → PIL Image, vstacks into one bitmap, sends via `image_to_gs_v_0()` to the registered receipt printer.
- On successful charge, manager itself prints slip if a receipt-kind printer is registered.
- Pros: no frontend change. Pro/con: requires a receipt printer to be registered (currently Yana's setup only has a label printer).

Option B — return parsed lines/bitmap, let frontend decide:
- Add `slip_bitmap_b64: Optional[str]` (rendered PIL Image as base64 PNG) alongside raw `terminal_receipt`.
- Frontend's existing `autoPrintNonFiscalReceipt` flow appends the slip after its own non-fiscal receipt.
- Pros: more flexible. Cons: more code.

**Recommendation:** Option A first (simpler, covers the immediate operator pain). Add Option B if/when frontend needs richer control.

### `_local_subnet()` — enumerate ALL interfaces for hotspot scenario

**Why:** Operator's tablet shares cellular as Wi-Fi hotspot; terminal connects to the hotspot. Tablet has two IPv4 addresses: cellular (primary outgoing, e.g. `10.X.X.X`) and hotspot AP (`192.168.43.1`). Our `_local_subnet()` uses UDP-connect-to-8.8.8.8 trick which returns the cellular IP, so we scan `10.X.X.0/24` and find nothing — terminal lives on the hotspot subnet.

**Implementation:** rewrite `_local_subnet() -> Optional[IPv4Network]` to `_local_subnets() -> list[IPv4Network]`. Use:
- On Linux/Android: parse `/proc/net/route` for all default-route-bearing interfaces, then read each interface's IP.
- Or via `socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET)` — gets all local IPs.
- Or shell out to `ip -4 addr show` (Termux has `ip` from net-tools or iproute2 if pkg installed).
- On macOS: `ifconfig` parse or `socket.getaddrinfo` variant.

`discover_network_terminals` iterates each /24, scans, deduplicates results.

### `_local_subnets()` test coverage

Once rewritten — pytest cases for the hotspot multi-interface scenario. Mock the interface enumeration, verify each /24 gets scanned.

## Medium value

### Android Companion APK for Bluetooth printer discovery

**Why:** Bluetooth printers are common (POS-58, mini portable thermals). On Android we currently `_is_termux()` → `return []` because there's no path to BluetoothAdapter from pure Python in Termux. A small APK that exposes `BluetoothAdapter.getBondedDevices()` via a local HTTP endpoint would unlock the same auto-discovery as desktop.

**Scope:** ~50KB Kotlin/Java APK, single Activity that runs a small HTTP server (e.g. NanoHTTPD) on `localhost:9998`. Endpoints:
- `GET /paired` — returns JSON list of paired BT devices with name + MAC.
- `POST /scan` — triggers `startDiscovery()`, returns list of new pairs.

`discover_bluetooth()` on Termux checks if companion is reachable, falls back to current "[]" if not.

Operator workflow: install APK once, grant Bluetooth permission once, then `Сканувати принтери` works on Android same as desktop.

### Self-test print button per printer

**Why:** When operator says "the printer isn't printing," half the time it's a paper feed issue, half it's our code. A "Test print" button per registered printer that sends a known-good ESC/POS test page lets the operator separate the two without us debugging.

**Implementation:** `POST /devices/{printer_id}/test-print` (already exists per `src/routes/devices.py`); add dashboard button that calls it. Should already work — just need the UI.

### Printer auto-discovery for Android: instructions for operator

Network printer discovery via mDNS (zeroconf) on Android is unreliable due to Android's strict multicast handling. Operator should ideally type the printer's static IP into the dashboard. Add an "Add network printer by IP" workflow (input box for IP + port, presets for common thermal printer ports 9100/9200).

### Document the X-Api-Key rotation procedure

Current API key (`bf11b47b-e139-4f03-8e02-9c2e692f91b8`) is hardcoded as `DEFAULT_API_KEY` and shipped with every install. Operators can override in `config.yaml`. Document in `docs/` how to rotate (generate new UUID → put in `config.yaml` → restart → update frontend's `BARHANDLER_MANAGER_API_KEY` constant → redeploy frontend).

Also: think about whether a default-shipped key makes sense long-term, or if installer should generate a per-install random one and print it to the operator.

## Low value / nice-to-have

### Replace the dashboard's polling with SSE

Dashboard polls `/health`, `/devices`, `/terminal` every 2 seconds. SSE (Server-Sent Events) would push only when state changes. Lower load, snappier UX. FastAPI has good SSE support.

### Auto-update channel for stable vs nightly

Some operators (especially in production stores) don't want every release. A `config.yaml` setting `update_channel: stable|latest` could pin them to tagged releases vs `production` HEAD.

### Health endpoint should include manager uptime + last-error timestamp

Useful for the operator to know "manager has been up for 3 days" or "last crash was 2 hours ago".

### Windows installer (`install.ps1`) — field test

Exists but never field-tested. Will probably break in 3 places on first real install. Worth a half-day to get someone running on Windows.

## Done — moved here from open list

(none yet — this is the initial list)
