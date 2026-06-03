# Agent context — barhandler-manager

This folder is a self-contained brief for a fresh agent (Claude Code / similar) that's never seen this project before. Read it in order before touching code.

## Why this exists

The manager went through ~30 releases in two days fixing accumulated installer and integration bugs across macOS / Termux / Android Chrome PWA. The lessons are non-obvious (`launchctl` lies about exit codes, Chrome 117+ PNA is opaquely named, Termux upgrades Python under your venv, etc.) and would cost another full day to re-derive from scratch. This folder is the cheat sheet.

## Reading order

1. **[architecture.md](./architecture.md)** — what this service does, how the SSI terminal protocol works, where data lives, repo layout.
2. **[platforms.md](./platforms.md)** — macOS / Linux / Termux quirks. The installer alone has 4 different code paths and 7 known landmines.
3. **[troubleshooting.md](./troubleshooting.md)** — diagnostic flowcharts for the recurring "doesn't work" tickets. Each entry has confirming tests and the patch that fixed it.
4. **[sessions.md](./sessions.md)** — chronological log of every release (v0.3.7 → v0.3.28+) with the bug each one fixed. Useful when investigating regressions.
5. **[open-tasks.md](./open-tasks.md)** — what's not done. Pick from here.

## Quick orientation

- **Language:** Python 3.11+ (FastAPI + uvicorn). Native deps via Homebrew on Mac, apt on Linux, pkg on Termux.
- **Hosted on:** the operator's own machine (Mac / mini-PC / Android tablet). Bridge between a browser-based POS frontend and physical USB/Bluetooth/network printers + POS terminals.
- **Entry point:** [`main.py`](../../main.py) → [`src/server.py`](../../src/server.py) → FastAPI app on `localhost:9999`.
- **Auth:** `X-Api-Key` header (`bf11b47b-e139-4f03-8e02-9c2e692f91b8` default; configurable in `config.yaml`).
- **Reference frontend:** `bar-handler-app` (Angular 20) — separate repo; serves the operator UI and calls this manager.

## What "done" looks like for a session

After non-trivial work, write a session entry into [`sessions.md`](./sessions.md). Future agents (including future-you) need the trail to understand why something is the way it is.

## SSI protocol spec

The authoritative source for the POS terminal protocol is [`docs/SSI-ECR-JSON-protocol-v1.3.2.pdf`](../SSI-ECR-JSON-protocol-v1.3.2.pdf) (in the parent `docs/` folder). The brief is in [architecture.md → SSI protocol](./architecture.md#ssi-ecr-json-protocol). When the PDF and reality disagree, the PDF wins — but the reference emulator (`Projects/BarHandler/SSI Json Emul 2/`) lags behind the spec, so don't use it as canonical.
