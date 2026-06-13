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
