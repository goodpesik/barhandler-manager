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
