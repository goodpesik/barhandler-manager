"""Operator dashboard served at GET / — a single self-contained HTML
page that polls the manager's own JSON endpoints and renders printers
+ POS terminals in a live-refreshing table.

The API key is rendered into the page so the JS can call the gated
routes. That's not a leak — the key is the same DEFAULT_API_KEY
constant shipped with every install. It's a low-effort handshake, not
a secret.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.constants import DEFAULT_API_KEY

router = APIRouter()


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>barhandler-manager</title>
<style>
  :root {
    --bg: #fafafa; --panel: #fff; --text: #1f2328; --muted: #6b7280;
    --border: #e5e7eb; --green: #1a7f37; --red: #cf222e; --amber: #bf8700;
    --blue: #0969da;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117; --panel: #161b22; --text: #e6edf3; --muted: #8d96a0;
      --border: #30363d; --green: #3fb950; --red: #f85149; --amber: #d29922;
      --blue: #58a6ff;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.45;
  }
  .wrap { max-width: 1200px; margin: 0 auto; }
  header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 16px; gap: 16px; flex-wrap: wrap;
  }
  h1 { margin: 0; font-size: 18px; display: flex; align-items: center; gap: 8px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); display: inline-block; }
  .dot.ok { background: var(--green); }
  .dot.err { background: var(--red); }
  .meta { color: var(--muted); font-size: 12px; display: flex; gap: 16px; flex-wrap: wrap; align-items: center; }
  .meta code { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  .controls {
    display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px;
    padding: 12px 16px;
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    align-items: center;
  }
  .controls-label { color: var(--muted); font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; margin-right: 4px; }
  .btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 6px 14px; border-radius: 6px; font-size: 13px; font-weight: 500;
    cursor: pointer; border: 1px solid transparent; transition: opacity 0.15s;
    white-space: nowrap;
  }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-default {
    background: var(--panel); border-color: var(--border); color: var(--text);
  }
  .btn-default:hover:not(:disabled) { border-color: var(--muted); }
  .btn-update {
    background: var(--amber); color: #fff; border-color: transparent;
  }
  .btn-update:hover:not(:disabled) { opacity: 0.85; }
  section {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; margin-bottom: 16px;
  }
  section h2 {
    margin: 0 0 12px 0; font-size: 14px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.04em;
    display: flex; align-items: center; justify-content: space-between;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); }
  tbody tr:last-child td { border-bottom: 0; }
  th { color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
  td.id { font-family: ui-monospace, "SF Mono", Menlo, monospace; color: var(--muted); font-size: 12px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .badge.ok  { background: rgba(63,185,80,.15); color: var(--green); }
  .badge.err { background: rgba(248,81,73,.15);  color: var(--red); }
  .badge.muted { background: var(--border); color: var(--muted); }
  .empty { color: var(--muted); font-style: italic; padding: 16px 12px; text-align: center; }
  .toast {
    position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 20px; font-size: 13px;
    box-shadow: 0 4px 16px rgba(0,0,0,.12); opacity: 0;
    transition: opacity 0.2s; pointer-events: none; white-space: nowrap;
    z-index: 100;
  }
  .toast.show { opacity: 1; }
  .toast.ok   { border-color: var(--green); color: var(--green); }
  .toast.err  { border-color: var(--red);   color: var(--red); }
  .update-strip {
    display: none; align-items: center; gap: 10px; flex-wrap: wrap;
    background: rgba(191,135,0,.1); border: 1px solid var(--amber);
    border-radius: 8px; padding: 10px 16px; margin-bottom: 16px;
    font-size: 13px; color: var(--amber);
  }
  .update-strip.show { display: flex; }
  .error-banner {
    background: rgba(248,81,73,.1); border: 1px solid var(--red);
    color: var(--red); padding: 12px 16px; border-radius: 8px;
    margin-bottom: 16px; display: none;
  }
  .error-banner.show { display: block; }
  footer { color: var(--muted); font-size: 11px; text-align: center; margin-top: 24px; }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1><span id="status-dot" class="dot"></span> barhandler-manager</h1>
    <div class="meta">
      <span>версія <code id="version">—</code></span>
      <span>оновлено <code id="updated">—</code></span>
    </div>
  </header>

  <div id="update-strip" class="update-strip">
    <span id="update-msg"></span>
    <button id="btn-update" class="btn btn-update" onclick="doUpdate()">⬆ Оновити</button>
  </div>

  <div id="error-banner" class="error-banner"></div>

  <div class="controls">
    <span class="controls-label">Дії</span>
    <button class="btn btn-default" onclick="scanPrinters(this)">🔍 Сканувати принтери</button>
    <button class="btn btn-default" onclick="scanTerminals(this)">🔍 Сканувати термінали</button>
    <button class="btn btn-default" onclick="openLogs()">📋 Логи</button>
    <button class="btn btn-default" onclick="runUsbProbe()">🔌 USB діагностика</button>
  </div>

  <section id="logs-panel" style="display:none;">
    <h2>
      Логи
      <span style="font-weight:normal; font-size:0.9rem; margin-left:1rem;">
        <button class="btn btn-default" data-log="bhm" onclick="loadLog('bhm', this)">bhm.log</button>
        <button class="btn btn-default" data-log="boot" onclick="loadLog('boot', this)">bhm.boot.log</button>
        <button class="btn btn-default" data-log="update" onclick="loadLog('update', this)">update.log</button>
        <button class="btn btn-default" onclick="refreshLog()">↻</button>
        <button class="btn btn-default" onclick="closeLogs()">✕</button>
      </span>
    </h2>
    <pre id="log-content"
         style="background:#111;color:#ddd;padding:1rem;border-radius:6px;overflow:auto;max-height:60vh;font-size:0.8rem;line-height:1.3;white-space:pre-wrap;word-break:break-all;">—</pre>
  </section>

  <section>
    <h2>
      Принтери / Printers
    </h2>
    <table>
      <thead>
        <tr><th>ID</th><th>Назва</th><th>Роль</th><th>Transport</th><th>Стан</th></tr>
      </thead>
      <tbody id="printers"><tr><td class="empty" colspan="5">завантаження…</td></tr></tbody>
    </table>
  </section>

  <section>
    <h2>POS-термінали / POS terminals</h2>
    <table>
      <thead>
        <tr><th>ID</th><th>Назва</th><th>Банк</th><th>Адреса</th><th>Default merchant</th></tr>
      </thead>
      <tbody id="terminals"><tr><td class="empty" colspan="5">завантаження…</td></tr></tbody>
    </table>
  </section>

  <footer>Polling /health, /devices, /terminal — кожні 2 секунди.</footer>
</div>

<div id="toast" class="toast"></div>

<script>
  const API_KEY = "__API_KEY__";
  const HEADERS = { "X-Api-Key": API_KEY, "Content-Type": "application/json" };
  const GITHUB_REPO = "goodpesik/barhandler-manager";

  const $ = (id) => document.getElementById(id);
  let currentVersion = null;
  let versionCheckTimer = null;

  // ---- fetch helpers -------------------------------------------------------

  async function api(method, path, withAuth) {
    const opts = { method };
    if (withAuth) opts.headers = HEADERS;
    const res = await fetch(path, opts);
    if (!res.ok) throw new Error(path + " → " + res.status);
    return res.json();
  }

  // ---- toast ---------------------------------------------------------------

  let toastTimer = null;
  function showToast(msg, kind = "ok", ms = 4000) {
    const el = $("toast");
    el.textContent = msg;
    el.className = "toast show " + kind;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = "toast"; }, ms);
  }

  // ---- rendering -----------------------------------------------------------

  function escHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  function badge(text, kind) {
    return '<span class="badge ' + kind + '">' + escHtml(text) + "</span>";
  }

  function renderPrinters(devices, health) {
    const hmap = {};
    (health?.printers || []).forEach((p) => { hmap[p.id] = p; });
    const rows = (devices?.printers || []).map((reg) => {
      const d = reg.descriptor || {};
      const status = (hmap[d.id] || {}).status || "unknown";
      const kind = status === "connected" ? "ok" : (status === "unknown" ? "muted" : "err");
      return "<tr>"
        + "<td class='id'>" + escHtml((d.id || "").slice(0, 12)) + "</td>"
        + "<td>" + escHtml(reg.nickname || d.label || "") + "</td>"
        + "<td>" + escHtml(reg.kind || "") + "</td>"
        + "<td>" + escHtml(d.transport || "") + "</td>"
        + "<td>" + badge(status, kind) + "</td>"
        + "</tr>";
    });
    $("printers").innerHTML = rows.length
      ? rows.join("")
      : '<tr><td class="empty" colspan="5">— не зареєстровано —</td></tr>';
  }

  function renderTerminals(terminals) {
    const rows = (terminals?.terminals || []).map((reg) => {
      const d = reg.descriptor || {};
      const net = d.network || {};
      const addr = net.host ? escHtml(net.host) + ":" + escHtml(net.port ?? "?") : escHtml(d.transport || "");
      return "<tr>"
        + "<td class='id'>" + escHtml((d.id || "").slice(0, 12)) + "</td>"
        + "<td>" + escHtml(reg.nickname || d.label || "") + "</td>"
        + "<td>" + escHtml(reg.kind || "") + "</td>"
        + "<td class='id'>" + addr + "</td>"
        + "<td>" + escHtml(reg.default_merchant_id || "—") + "</td>"
        + "</tr>";
    });
    $("terminals").innerHTML = rows.length
      ? rows.join("")
      : '<tr><td class="empty" colspan="5">— не зареєстровано —</td></tr>';
  }

  // ---- version check (GitHub, once per 5 min) ------------------------------

  function semverGt(a, b) {
    const p = (v) => v.replace(/^v/, "").split(".").map(Number);
    const [av, bv] = [p(a), p(b)];
    for (let i = 0; i < 3; i++) {
      if ((av[i] || 0) > (bv[i] || 0)) return true;
      if ((av[i] || 0) < (bv[i] || 0)) return false;
    }
    return false;
  }

  async function checkLatestVersion() {
    if (!currentVersion || currentVersion === "?") return;
    try {
      const data = await fetch(
        "https://api.github.com/repos/" + GITHUB_REPO + "/releases/latest",
        { headers: { Accept: "application/vnd.github+json" } }
      ).then((r) => r.json());
      const latest = (data.tag_name || "").replace(/^v/, "");
      if (latest && semverGt(latest, currentVersion)) {
        $("update-msg").textContent =
          "Доступна нова версія v" + latest + " (зараз v" + currentVersion + ")";
        $("update-strip").className = "update-strip show";
      } else {
        $("update-strip").className = "update-strip";
      }
    } catch (_) { /* GitHub API недоступний — тихо ігноруємо */ }
  }

  // ---- scan ----------------------------------------------------------------

  async function scanPrinters(btn) {
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const res = await api("POST", "/devices/discover", true);
      const n = (res.printers || []).length;
      showToast("Знайдено принтерів: " + n, "ok");
      await refresh();
    } catch (e) {
      showToast("Помилка сканування: " + e.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "🔍 Сканувати принтери";
    }
  }

  // ---- logs panel ----------------------------------------------------------

  let currentLog = "bhm";

  function openLogs() {
    $("logs-panel").style.display = "block";
    loadLog(currentLog);
  }

  function closeLogs() {
    $("logs-panel").style.display = "none";
  }

  async function loadLog(source, btn) {
    currentLog = source;
    $("log-content").textContent = "Завантаження…";
    try {
      const res = await api("GET", "/system/logs?source=" + source + "&tail=500", true);
      if (!res.exists) {
        $("log-content").textContent = `(${res.path} ще не існує)`;
        return;
      }
      const lines = res.lines || [];
      $("log-content").textContent = lines.length
        ? lines.join("\n")
        : "(порожньо)";
      const el = $("log-content");
      el.scrollTop = el.scrollHeight;
    } catch (e) {
      $("log-content").textContent = "Помилка: " + (e.message || e);
    }
  }

  function refreshLog() {
    loadLog(currentLog);
  }

  async function runUsbProbe() {
    $("logs-panel").style.display = "block";
    $("log-content").textContent = "Запуск USB діагностики…";
    try {
      const res = await api("POST", "/system/usb-probe", true);
      const out = res.stdout || "";
      const err = res.stderr || "";
      $("log-content").textContent =
        out + (err ? "\n--- stderr ---\n" + err : "");
    } catch (e) {
      $("log-content").textContent = "Помилка: " + (e.message || e);
    }
  }

  async function scanTerminals(btn) {
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const res = await api("POST", "/terminal/discover", true);
      const n = (res.terminals || []).length;
      showToast("Знайдено терміналів: " + n, "ok");
      await refresh();
    } catch (e) {
      showToast("Помилка сканування: " + e.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "🔍 Сканувати термінали";
    }
  }

  // ---- update --------------------------------------------------------------

  async function doUpdate() {
    const btn = $("btn-update");
    btn.disabled = true;
    btn.textContent = "Запускаємо…";
    try {
      const res = await api("POST", "/system/update", true);
      showToast(res.message || "Оновлення запущено!", "ok", 10000);
      btn.textContent = "Перезапуск…";
      // Poll update.log so the operator sees real progress (curl
      // downloads, pip output, launchctl reload). If we go silent for
      // 60s with the manager still alive, surface the log so they
      // know why the update isn't taking.
      pollUpdateLog();
    } catch (e) {
      showToast("Помилка оновлення: " + e.message, "err");
      btn.disabled = false;
      btn.textContent = "⬆ Оновити";
    }
  }

  async function pollUpdateLog() {
    const started = Date.now();
    const POLL_MS = 3000;
    const REPORT_AFTER_MS = 60000;
    let reported = false;
    const tick = async () => {
      try {
        const log = await api("GET", "/system/update-log?tail=30", true);
        const elapsed = Date.now() - started;
        if (elapsed > REPORT_AFTER_MS && !reported && log.exists) {
          reported = true;
          const last = (log.lines || []).slice(-6).join("\n");
          showToast(
            "Оновлення ще йде, останні рядки логу:\n" + last,
            "ok", 15000,
          );
        }
      } catch (_) {
        // /health throws too when the manager restarts — that's the
        // happy path. Stop polling.
        return;
      }
      // Stop once the manager itself reboots (next /health call will
      // fail and trigger error banner via the main refresh loop).
      setTimeout(tick, POLL_MS);
    };
    setTimeout(tick, POLL_MS);
  }

  // ---- main poll loop ------------------------------------------------------

  async function refresh() {
    try {
      const health = await api("GET", "/health", false);
      const ver = health.version || "?";
      $("version").textContent = "v" + ver;
      if (currentVersion !== ver) {
        currentVersion = ver;
        checkLatestVersion();
      }
      let devices, terminals;
      try { devices = await api("GET", "/devices", true); } catch (_) { devices = null; }
      try { terminals = await api("GET", "/terminal", true); } catch (_) { terminals = null; }
      renderPrinters(devices, health);
      renderTerminals(terminals);
      $("updated").textContent = new Date().toLocaleTimeString("uk-UA");
      $("status-dot").className = "dot ok";
      $("error-banner").className = "error-banner";
    } catch (e) {
      $("status-dot").className = "dot err";
      $("error-banner").className = "error-banner show";
      $("error-banner").textContent = "Менеджер недоступний: " + (e.message || e);
    }
  }

  refresh();
  setInterval(refresh, 2000);
  // Version check every 5 minutes after the first (triggered inside refresh on ver change).
  setInterval(checkLatestVersion, 5 * 60 * 1000);
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(_request: Request) -> HTMLResponse:
    body = _HTML_TEMPLATE.replace("__API_KEY__", DEFAULT_API_KEY)
    return HTMLResponse(body)
