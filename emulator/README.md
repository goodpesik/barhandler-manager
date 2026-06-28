# Emulators (local test tools)

This package holds two device-side emulators so you can exercise the full
manager flow with no hardware:

| Run | Emulates | What you do |
|---|---|---|
| `python -m emulator` | **POS terminal** (SSI ECR JSON) | approve / decline / cancel charges from a console menu |
| `python -m emulator.printer` | **ESC/POS thermal printer** (RAW/9100) | watch receipts render live in your browser |

---

# ESC/POS printer emulator (local test tool)

Acts as a **network thermal printer**: it listens on the raw-9100 print
port exactly like a real ESC/POS printer, so the manager's python-escpos
`Network` driver prints to it **without any manager-side change**. Every
job is reconstructed pixel-for-pixel into a PNG and shown on a **live web
page** — newest receipt on top. Nothing is written to disk; receipts live
in memory only.

It supports both paper modes the manager drives — **58mm (384 dots)** and
**80mm (576 dots)** — and *auto-detects* which one from the `GS v 0`
raster header, so you don't have to configure it. The default **bitmap**
render mode (every glyph + the fiscal QR rasterised) reproduces exactly;
the `native` code-page text mode is decoded best-effort for the preview.

> **Local only.** Not imported by the manager app, not wired into any
> route, not for production.

## Install & run

```bash
cd barhandler-manager
pip install -r emulator/requirements.txt
python -m emulator.printer            # RAW sink on 0.0.0.0:9100, viewer on :8089
```

Open the viewer it prints (default <http://127.0.0.1:8089>) and leave it
up. Options: `--host`/`--port` (RAW sink, default `0.0.0.0:9100`),
`--web-host`/`--web-port` (viewer, default `127.0.0.1:8089`),
`--paper {58,80}` (fallback width when a job has no raster),
`--manager-port`, `--register`.

## Point the manager at it

The manager finds network printers by scanning its **LAN /24** (loopback
is skipped), so the realistic flow is discover → register:

```bash
# 1) discover — the emulator answers on the host's LAN IP:9100
curl -s -X POST http://127.0.0.1:9999/devices/discover \
  -H "X-Api-Key: bf11b47b-e139-4f03-8e02-9c2e692f91b8" | jq

# 2) register the returned network-printer id
curl -X POST http://127.0.0.1:9999/devices/register \
  -H "X-Api-Key: bf11b47b-e139-4f03-8e02-9c2e692f91b8" \
  -H "Content-Type: application/json" \
  -d '{"id":"<id>","kind":"receipt","paper_width":58}'
```

**Localhost, no LAN?** Discovery can't see `127.0.0.1`, so seed the
registration directly and restart the manager:

```bash
python -m emulator.printer --register printers.json    # writes a 127.0.0.1:9100 entry
```

Then print from the app (or `POST /devices/<id>/test-print`) and the
receipt appears in the viewer instantly.

## How it works

The manager's bitmap pipeline emits each line — and the fiscal QR — as a
single `GS v 0` raster (`src/services/bitmap_render.image_to_gs_v_0`), so
that's the only image command on the wire. The emulator:

1. Parses the ESC/POS stream (handling partial commands across TCP reads).
2. Decodes every `GS v 0` back into a 1-bit bitmap and stacks them.
3. On the paper cut (`GS V`) finalises the stack into one PNG receipt.
4. Answers the manager's pre-flight status queries (`DLE EOT 1` / `DLE EOT
   4`) with a single `0x12` byte → *online + paper adequate*; without this
   the manager's `is_online()` check refuses to print.

Wire-level detail and unhandled-command logging go to
`printer-emulator.log`.

---

# POS terminal emulator (local test tool)

Emulates the **terminal side** of the SSI ECR JSON protocol (Mono / Raif /
Pivdenny / generic-SSI) so you can exercise the full `barhandler-manager →
terminal` flow — discover, register, charge, approve/decline/cancel — with no
real hardware.

It is byte-compatible with `src/services/terminals/ssi.py` (same STX/LEN/LRC
framing) and re-creates Mono's two-step Purchase. For every charge it shows an
arrow-key menu where **you** decide the outcome.

> **Local only.** Not imported by the manager app, not wired into any route,
> not for production.

## Install & run

```bash
cd barhandler-manager
pip install -r emulator/requirements.txt
python -m emulator                 # listens on 127.0.0.1:3000
```

Options: `--host`, `--port` (default 3000), `--manager-port` (default 9999),
`--merchant-id`, `--merchant-name`, `--terminal-id`.

## Point the manager at it

Discovery scans the LAN, so for a localhost emulator register it manually:

```bash
curl -X POST http://127.0.0.1:9999/terminal/register-manual \
  -H "X-Api-Key: bf11b47b-e139-4f03-8e02-9c2e692f91b8" \
  -H "Content-Type: application/json" \
  -d '{"host":"127.0.0.1","port":3000,"kind":"mono_pos","nickname":"Емулятор"}'
```

(`X-Api-Key` is the manager's `DEFAULT_API_KEY`; override if your `config.yaml`
sets a custom one.) The terminal then shows up in the app's payment modal like
a real one.

## Use it

1. Start the emulator, register it (above).
2. In the app: card payment → pick the emulator terminal → Pay.
3. The emulator console shows the amount and an arrow-key menu:
   **Approve / Decline / Cancel** (↑/↓, Enter).
4. The app receives the matching outcome (approved / declined / cancelled).

Wire traffic is logged to `emulator.log` (`tail -f emulator.log`).

## What it implements (SSI, device side)

`PingDevice`, `GetTerminalInfo`, `GetMerchantListDetailed`, `Purchase`
(step 1 → S08 → step 2 → S00), `GetStatus`, `GetLastResult` /
`GetResultByUid`, `GetLastReceipt`, `Interrupt`.

PrivatBank PB-JSON (TCP 2000, `\x00`-terminated) is a separate protocol and
not implemented yet — add a `pb_terminal.py` alongside `ssi_terminal.py`.
