"""Operator dashboard served at GET / — a single self-contained HTML
page that polls the manager's own JSON endpoints and renders printers
+ POS terminals in a live-refreshing table.

Why HTML and not a separate frontend project: the dashboard is for the
local operator (or the engineer SSH-forwarding 9999), and the data is
already exposed by /health + /devices + /terminal. A small inline page
avoids dragging in a build step and a static-files convention for one
view. Refreshes every 2 seconds via fetch.

The API key is rendered into the page so the JS can call the gated
routes. That's not a leak — the key is the same `DEFAULT_API_KEY`
constant shipped with every install (the BarHandler / FitStudio
frontends ship it client-side too). It's a low-effort handshake, not
a secret.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.constants import DEFAULT_API_KEY

router = APIRouter()


_HTML_TEMPLATE = """<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>barhandler-manager dashboard</title>
<style>
  :root {
    --bg: #fafafa;
    --panel: #fff;
    --text: #1f2328;
    --muted: #6b7280;
    --border: #e5e7eb;
    --green: #1a7f37;
    --red: #cf222e;
    --amber: #bf8700;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117;
      --panel: #161b22;
      --text: #e6edf3;
      --muted: #8d96a0;
      --border: #30363d;
      --green: #3fb950;
      --red: #f85149;
      --amber: #d29922;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 24px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    line-height: 1.45;
  }
  .wrap { max-width: 1200px; margin: 0 auto; }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 24px;
    gap: 16px;
    flex-wrap: wrap;
  }
  h1 {
    margin: 0;
    font-size: 18px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--muted);
    display: inline-block;
  }
  .dot.ok { background: var(--green); }
  .dot.err { background: var(--red); }
  .meta {
    color: var(--muted);
    font-size: 12px;
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
  }
  .meta code { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  section {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 16px;
  }
  section h2 {
    margin: 0 0 12px 0;
    font-size: 14px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  th, td {
    text-align: left;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
  }
  tbody tr:last-child td { border-bottom: 0; }
  th {
    color: var(--muted);
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  td.id {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    color: var(--muted);
    font-size: 12px;
  }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
  }
  .badge.ok { background: rgba(63, 185, 80, 0.15); color: var(--green); }
  .badge.err { background: rgba(248, 81, 73, 0.15); color: var(--red); }
  .badge.muted { background: var(--border); color: var(--muted); }
  .empty {
    color: var(--muted);
    font-style: italic;
    padding: 16px 12px;
    text-align: center;
  }
  .error-banner {
    background: rgba(248, 81, 73, 0.1);
    border: 1px solid var(--red);
    color: var(--red);
    padding: 12px 16px;
    border-radius: 8px;
    margin-bottom: 16px;
    display: none;
  }
  .error-banner.show { display: block; }
  footer {
    color: var(--muted);
    font-size: 11px;
    text-align: center;
    margin-top: 24px;
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>
      <span id="status-dot" class="dot"></span>
      barhandler-manager
    </h1>
    <div class="meta">
      <span><code id="version">—</code></span>
      <span>updated <code id="updated">—</code></span>
    </div>
  </header>

  <div id="error-banner" class="error-banner"></div>

  <section>
    <h2>Принтери / Printers</h2>
    <table>
      <thead>
        <tr>
          <th>ID</th><th>Назва</th><th>Роль</th><th>Transport</th><th>Стан</th>
        </tr>
      </thead>
      <tbody id="printers"><tr><td class="empty" colspan="5">завантаження…</td></tr></tbody>
    </table>
  </section>

  <section>
    <h2>POS-термінали / POS terminals</h2>
    <table>
      <thead>
        <tr>
          <th>ID</th><th>Назва</th><th>Банк</th><th>Адреса</th><th>Default merchant</th>
        </tr>
      </thead>
      <tbody id="terminals"><tr><td class="empty" colspan="5">завантаження…</td></tr></tbody>
    </table>
  </section>

  <footer>
    Оновлюється кожні 2 секунди. Live-полінг до /health, /devices, /terminal.
  </footer>
</div>

<script>
  const API_KEY = "__API_KEY__";
  const HEADERS = { "X-Api-Key": API_KEY };
  const REFRESH_MS = 2000;

  const $ = (id) => document.getElementById(id);
  const dot = $("status-dot");
  const banner = $("error-banner");

  async function fetchJson(path, withAuth) {
    const res = await fetch(path, withAuth ? { headers: HEADERS } : undefined);
    if (!res.ok) throw new Error(path + " → " + res.status);
    return res.json();
  }

  function badge(text, kind) {
    return `<span class="badge ${kind}">${text}</span>`;
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\\"": "&quot;", "'": "&#39;",
    }[c]));
  }

  function renderPrinters(devices, health) {
    const healthMap = {};
    (health?.printers || []).forEach((p) => { healthMap[p.id] = p; });
    const rows = (devices?.printers || []).map((reg) => {
      const d = reg.descriptor || {};
      const status = (healthMap[d.id] || {}).status || "unknown";
      const kind = status === "connected" ? "ok" : (status === "unknown" ? "muted" : "err");
      return `<tr>
        <td class="id">${escapeHtml(d.id?.slice(0, 12) || "")}</td>
        <td>${escapeHtml(reg.nickname || d.label || "")}</td>
        <td>${escapeHtml(reg.kind || "")}</td>
        <td>${escapeHtml(d.transport || "")}</td>
        <td>${badge(status, kind)}</td>
      </tr>`;
    });
    $("printers").innerHTML = rows.length
      ? rows.join("")
      : `<tr><td class="empty" colspan="5">— не зареєстровано —</td></tr>`;
  }

  function renderTerminals(terminals) {
    const rows = (terminals?.terminals || []).map((reg) => {
      const d = reg.descriptor || {};
      const net = d.network || {};
      const addr = net.host
        ? `${escapeHtml(net.host)}:${escapeHtml(net.port ?? "?")}`
        : escapeHtml(d.transport || "");
      return `<tr>
        <td class="id">${escapeHtml(d.id?.slice(0, 12) || "")}</td>
        <td>${escapeHtml(reg.nickname || d.label || "")}</td>
        <td>${escapeHtml(reg.kind || "")}</td>
        <td class="id">${addr}</td>
        <td>${escapeHtml(reg.default_merchant_id || "—")}</td>
      </tr>`;
    });
    $("terminals").innerHTML = rows.length
      ? rows.join("")
      : `<tr><td class="empty" colspan="5">— не зареєстровано —</td></tr>`;
  }

  function setOk() {
    dot.className = "dot ok";
    banner.className = "error-banner";
    banner.textContent = "";
  }

  function setErr(msg) {
    dot.className = "dot err";
    banner.className = "error-banner show";
    banner.textContent = "Менеджер недоступний: " + msg;
  }

  async function refresh() {
    try {
      const health = await fetchJson("/health", false);
      $("version").textContent = "v" + (health.version || "?");
      let devices, terminals;
      try { devices = await fetchJson("/devices", true); } catch (_) { devices = null; }
      try { terminals = await fetchJson("/terminal", true); } catch (_) { terminals = null; }
      renderPrinters(devices, health);
      renderTerminals(terminals);
      $("updated").textContent = new Date().toLocaleTimeString("uk-UA");
      setOk();
    } catch (e) {
      setErr(e.message || e);
    }
  }

  refresh();
  setInterval(refresh, REFRESH_MS);
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(_request: Request) -> HTMLResponse:
    """Single-file HTML dashboard. Polls /health (no auth), /devices
    and /terminal (with the bundled API key) every 2s and renders
    printer + terminal tables. Lives at the root path so a fresh
    install visit to `http://localhost:9999` lands here instead of
    a 404."""
    api_key = DEFAULT_API_KEY
    # Naive but sufficient substitution — the placeholder is unique
    # and the key is a known constant; we don't want jinja2 just for
    # one substitution.
    body = _HTML_TEMPLATE.replace("__API_KEY__", api_key)
    return HTMLResponse(body)
