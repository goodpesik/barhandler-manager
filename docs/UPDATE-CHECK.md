# Update check — frontend integration

The manager polls GitHub Releases (`goodpesik/barhandler-manager`) once
an hour in the background and embeds the result in two endpoints. The
frontend uses this to surface an "Update available" modal that, on
click, calls the existing `POST /system/update` endpoint to trigger the
update.

## Endpoints

Both endpoints are **no-auth** (`/health` and `/version`). Use whichever
you're already polling.

### `GET /version`

Dedicated endpoint, smaller response.

```json
{
  "version": "0.3.0",
  "latest_version": "0.3.28",
  "has_update": true,
  "release_url": "https://github.com/goodpesik/barhandler-manager/releases/tag/v0.3.28",
  "release_published_at": "2026-06-02T17:43:56Z",
  "checked_at": "2026-06-03T07:49:14.707632+00:00"
}
```

### `GET /health`

Same fields, plus the existing `status` + `printers[]` payload. If you
already poll `/health` for printer status pills, you don't need a second
round-trip — just read the same fields off the same response.

## Field reference

| Field | Type | Meaning |
|---|---|---|
| `version` | string | The version the manager is currently running. Read from the `VERSION` file written by `update.sh`. |
| `latest_version` | string \| null | The tag of the latest GitHub release (without the `v` prefix). `null` on a fresh boot before the first poll completes, or if GitHub is unreachable. |
| `has_update` | boolean | `true` iff `latest_version > version` (X.Y.Z compare). Always `false` when `latest_version` is `null` — absence of confirmation is treated as "no update", not "unknown". |
| `release_url` | string \| null | HTML URL of the release on GitHub. Open in a new tab for changelog. |
| `release_published_at` | string \| null | ISO 8601 timestamp from GitHub. |
| `checked_at` | string \| null | ISO 8601 timestamp of when the manager last successfully polled GitHub. Useful to surface "checked 12 min ago" in the modal. |

## Suggested frontend flow

1. While the manager is connected (you already detect this through
   `/health`), poll one of the two endpoints on whatever cadence you
   already use (every 5-30 s).
2. When `has_update === true` and no modal has been shown this session,
   pop the modal:
   ```
   Доступна нова версія менеджера
   Поточна: 0.3.0
   Нова:    0.3.28  (release notes ↗)
   [ Оновити ]   [ Пізніше ]
   ```
3. On **Оновити** click — call the existing protected endpoint:
   ```
   POST /system/update
   X-Api-Key: <bf11b47b-...>
   ```
   This is exactly what the dashboard's own "Оновити" button does. The
   manager spawns `update.sh` in the background, restarts itself, and
   the frontend will start failing the next health poll for ~10-30 s.
   When `/health` comes back with the new `version`, dismiss the modal.

## Suppressing "later" until next release

If the user clicks **Пізніше**, store the dismissed version
(e.g. `localStorage.setItem('bhm.dismissedUpdate', latest_version)`).
Only re-show the modal when `latest_version` is **different** from the
dismissed one. That way every new release re-prompts but the user isn't
nagged about a release they already declined.

## Edge cases

- **`latest_version` is `null` indefinitely** — GitHub blocked the
  manager (unauthenticated rate-limit is 60 req/hr per IP; the manager
  polls 24×/day so this is unusual). Frontend should treat as "no
  update" and not nag. Operator can manually `update.sh` if needed.
- **`current_version` is `0.0.0`** — `VERSION` file missing or
  unreadable. Treat the install as broken; surface a separate banner
  rather than the update modal.
- **Polling /version every second is fine** — it's a hot path that
  reads cached in-memory state; no GitHub call per request. Same for
  `/health`.
