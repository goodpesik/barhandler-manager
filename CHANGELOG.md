# Changelog

All notable changes ship here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) —
patch for bugfixes, minor for additive endpoints / features, major
when an old client would break.

The release workflow (`.github/workflows/publish.yml`) appends the
`## [Unreleased]` block to the auto-generated release notes for every
push to `production`. Move entries from `[Unreleased]` to a versioned
block in the same PR that bumps `VERSION`.

## [Unreleased]

### Added
- Multi-bank POS terminal support beyond Mono/Privat: **Printec PosAPI** (Raiffeisen / PUMB), **BPOS1 / BPOS Light** (Bank Pivdenny / Sense), and **Oschadbank ECR** adapters, each behind the same `TerminalAdapter` ABC and registry factory. New `TerminalKind`s (`pumb_pos`, `sense_pos`, `oschad_pos`, `generic_posapi`, `generic_bpos`); terminal LAN discovery now scans/probes ports 3000/2000/8080/8888/7777. Printec/BPOS/Oschad wire specs are partner-gated — the adapters are a best-effort model marked `# SPEC:`, validated end-to-end by the bundled emulator.
- Dashboard now **shows discovered devices** (printers and terminals) after a scan and lets you register each one in place — the found item syncs to the web POS app once registered. Manual-terminal modal covers the new banks with per-kind default-port auto-fill.
- Multi-bank **terminal emulator**: `python -m emulator` lets you pick which bank to emulate (SSI / Privat / PosAPI / BPOS / Oschad) at startup and switch banks while idle. `tests/test_emulator_roundtrip.py` drives the real adapters against every emulator to keep them in lock-step.
- README: step-by-step macOS/Windows install walkthroughs for non-technical operators, and the full bank-protocol matrix.
- USB discovery now finds cheap 58 mm thermal clones (SP-POS58IV, Rongta RG-P58D and other STMicro / Winbond / Zjiang units) that enumerate as vendor-specific class `0xff` — the scan accepts class `0xff`/`0x00` interfaces with bulk in+out endpoints, plus any known thermal-printer VID, on top of the standard Printer class `0x07`.
- `POST /devices/register-usb-manual` — register a USB printer by explicit VID/PID/endpoints (read off `scripts/usb_probe.py`) when even the relaxed scan can't see it or CUPS is holding the device. Mirrors the network-only `/devices/register-manual`.
- Public install pipeline: one-line installers for macOS / Linux / Raspberry Pi (`install.sh`), Windows (`install.ps1`), Android Termux (`install-android.sh`). Each script is idempotent — re-running upgrades to the latest release without touching `config.yaml` / `printers.json`, and drops `start` / `stop` / `status` helpers next to the install.
- LAN printer discovery (mDNS browse + `/24` raw-9100 port scan) and best-effort Bluetooth discovery on Linux.
- `/print/lines` endpoint for structured per-line formatted output (bold / centred / double-height) — bill and non-fiscal receipts render the same headlines on paper as the operator sees on screen.
- Rotating log file (`bhm.log`, 5 MB × 5 backups by default).

### Changed
- Kitchen ticket renders one self-contained block per item (position number + name + measurement + table + guest) with ~3 cm tear-off padding so single-item tickets stay on the rail clip.
- Fiscal receipt layout: full-width banner, СУМА spans the full line, QR code centred via the bitmap pipeline, padding so the printer's cutter doesn't shave the header.
- API key moved from `config.yaml` into a shared constant (`src/constants.py`) — same UUID lives in BarHandler's frontend. The handshake is a magic-string sentinel, not a secret; the config-file override is kept for hosts running multiple isolated POS apps.

### Fixed
- Cyrillic glyphs no longer print as `?` — bitmap rendering through Noto Sans Mono via `GS v 0` raster bypasses code-page mismatches on cheap ESC/POS clones.
- CORS preflight 405 from browser-side requests (added `CORSMiddleware` with sensible dev + prod allowlist).
- `POST /devices/register` response envelope unwrapping — the manager returns `{ "printer": {...} }` but the frontend was reading the registration fields off the top level and crashed silently.

## [0.2.0] — initial public preview

Pre-release tag held by the seed of the `production` branch — features
will land here when the next PR cuts the first real release.
