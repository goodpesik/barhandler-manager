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
<title>Handler Device Manager</title>
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
  .lang-toggle { display: inline-flex; align-items: center; gap: 4px; }
  .lang-btn {
    background: none; border: none; color: var(--muted); cursor: pointer;
    font-size: 12px; font-weight: 600; padding: 2px 4px; font-family: inherit;
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .lang-btn:hover { color: var(--text); }
  .lang-btn.active { color: var(--blue); text-decoration: underline; }
  .lang-sep { color: var(--border); }
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
  .btn-danger {
    background: transparent; color: #ef4444; border-color: rgba(239,68,68,0.4);
    padding: 4px 8px; font-size: 12px;
  }
  .btn-danger:hover:not(:disabled) { background: rgba(239,68,68,0.1); }
  .modal-backdrop {
    position: fixed; inset: 0; background: rgba(0,0,0,0.45);
    display: flex; align-items: center; justify-content: center;
    z-index: 50;
  }
  .modal {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 24px; max-width: 480px; width: 92%;
    box-shadow: 0 24px 48px rgba(0,0,0,0.2);
  }
  .modal h2 { margin: 0 0 8px 0; font-size: 16px; }
  .modal-desc { color: var(--muted); font-size: 13px; margin: 0 0 16px 0; }
  .modal-desc code { background: rgba(127,127,127,0.15); padding: 1px 4px; border-radius: 3px; }
  .modal-row {
    display: flex; flex-direction: column; gap: 4px;
    margin-bottom: 12px; font-size: 13px;
  }
  .modal-row > span:first-child { color: var(--muted); font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
  .modal-row input {
    padding: 8px 10px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--bg); color: var(--text);
    font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 13px;
  }
  .modal-row input:focus { outline: 2px solid var(--blue); outline-offset: -1px; }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
  .muted { color: var(--muted); }
  .ok-text { color: var(--green); font-weight: 600; }
  .err-text { color: var(--red); font-weight: 600; }
  .badge {
    display: inline-block; margin-left: 6px; padding: 1px 6px;
    border-radius: 4px; font-size: 11px; font-weight: 600;
    background: rgba(63, 185, 80, 0.18); color: var(--green);
  }
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
    <h1><span id="status-dot" class="dot"></span> Handler Device Manager</h1>
    <div class="meta">
      <span><span data-i18n="version_label">версія</span> <code id="version">—</code></span>
      <span><span data-i18n="updated_label">оновлено</span> <code id="updated">—</code></span>
      <span class="lang-toggle">
        <button id="lang-uk" class="lang-btn" type="button" onclick="setLang('uk')">UK</button>
        <span class="lang-sep">|</span>
        <button id="lang-en" class="lang-btn" type="button" onclick="setLang('en')">EN</button>
      </span>
    </div>
  </header>

  <div id="update-strip" class="update-strip">
    <span id="update-msg"></span>
    <button id="btn-update" class="btn btn-update" data-i18n="btn_update" onclick="doUpdate()">⬆ Оновити</button>
  </div>

  <div id="error-banner" class="error-banner"></div>

  <div class="controls">
    <span class="controls-label" data-i18n="controls_actions">Дії</span>
    <button class="btn btn-default" data-i18n="btn_refresh_status" onclick="refreshStatus(this)">↻ Оновити статус</button>
    <button class="btn btn-default" data-i18n="btn_scan_printers" onclick="scanPrinters(this)">🔍 Сканувати принтери</button>
    <button class="btn btn-default" data-i18n="btn_scan_terminals" onclick="scanTerminals(this)">🔍 Сканувати термінали</button>
    <button class="btn btn-default" data-i18n="btn_add_manual" onclick="openManualTerminalModal()">➕ Додати термінал вручну</button>
    <button class="btn btn-default" data-i18n="btn_add_printer_manual" onclick="openManualPrinterModal()">➕ Додати фіскальний принтер</button>
    <button class="btn btn-default" data-i18n="btn_logs" onclick="openLogs()">📋 Логи</button>
    <button class="btn btn-default" data-i18n="btn_usb" onclick="runUsbProbe()">🔌 USB діагностика</button>
    <button id="btn-uplink" class="btn btn-default" onclick="openUplinkModal()"><span data-i18n="btn_uplink">📡 Віддалена діагностика</span><span id="uplink-badge" class="badge" data-i18n="uplink_badge_connected" style="display:none;">● підключено</span></button>
  </div>

  <div id="manual-terminal-modal" class="modal-backdrop" style="display:none;">
    <div class="modal">
      <h2 data-i18n="mt_title">Додати термінал вручну</h2>

      <div style="display:flex; gap:8px; margin-bottom:14px;">
        <button id="mt-tabbtn-network" class="btn btn-default" data-i18n="mt_tab_network" onclick="mtTab('network')">🌐 Мережа</button>
        <button id="mt-tabbtn-usb" class="btn btn-default" data-i18n="mt_tab_usb" onclick="mtTab('usb')">🔌 USB</button>
      </div>

      <!-- NETWORK tab -->
      <div id="mt-tab-network">
        <p class="modal-desc" data-i18n="mt_desc">
          Резерв на випадок коли скан не побачив (CGNAT мобільного
          оператора, hotspot планшета з нестандартним subnet, окрема
          VLAN). IP терміналу дивись у його admin меню.
        </p>
        <label class="modal-row">
          <span data-i18n="mt_host_label">IP / Host</span>
          <input id="mt-host" type="text" data-i18n-ph="mt_host_ph" placeholder="10.245.122.201" />
        </label>
        <label class="modal-row">
          <span data-i18n="mt_port_label">Port</span>
          <input id="mt-port" type="number" value="3000" />
        </label>
        <label class="modal-row">
          <span data-i18n="mt_bank_label">Банк</span>
          <select id="mt-kind" onchange="mtKindChanged()">
            <optgroup label="SSI (Servus)">
              <option value="mono_pos">Monobank — SSI</option>
              <option value="generic_ssi">Інший SSI / Other SSI</option>
            </optgroup>
            <optgroup label="PrivatBank JSON">
              <option value="privat_pos">ПриватБанк — JSON</option>
            </optgroup>
            <optgroup label="Printec PosAPI">
              <option value="raif_pos">Райффайзен — PosAPI</option>
              <option value="pumb_pos">ПУМБ — PosAPI</option>
              <option value="generic_posapi">Інший PosAPI</option>
            </optgroup>
            <optgroup label="BPOS">
              <option value="pivdenny_pos">Південний — BPOS1</option>
              <option value="sense_pos">Sense / Альфа — BPOS</option>
              <option value="generic_bpos">Інший BPOS</option>
            </optgroup>
            <optgroup label="Oschad ECR">
              <option value="oschad_pos">Ощадбанк — ECR</option>
            </optgroup>
          </select>
        </label>
        <label class="modal-row">
          <span data-i18n="mt_nickname_label">Псевдонім (опційно)</span>
          <input id="mt-nickname" type="text" data-i18n-ph="mt_nickname_ph" placeholder="Бар / Каса 1" />
        </label>
        <div class="modal-actions">
          <button class="btn btn-default" data-i18n="btn_cancel" onclick="closeManualTerminalModal()">Скасувати</button>
          <button id="mt-save" class="btn btn-update" data-i18n="btn_mt_save" onclick="saveManualTerminal()">Додати</button>
        </div>
      </div>

      <!-- USB tab -->
      <div id="mt-tab-usb" style="display:none;">
        <p class="modal-desc" data-i18n="mt_usb_desc">
          USB-термінал (ПриватБанк) на Windows — це віртуальний COM-порт.
          Натисни «Пошук», обери порт і зареєструй. Термінал має бути в
          режимі ПК-USB-ECR (увімкнути через підтримку ПриватБанку).
        </p>
        <div style="margin-bottom:12px;">
          <button id="mt-serial-scan" class="btn btn-default" data-i18n="mt_usb_scan" onclick="scanSerialPorts(this)">🔍 Пошук USB-терміналів</button>
        </div>
        <label class="modal-row">
          <span data-i18n="mt_usb_port_label">COM-порт</span>
          <select id="mt-serial-port"><option value="" data-i18n="mt_usb_no_ports">— натисни «Пошук» —</option></select>
        </label>
        <label class="modal-row">
          <span data-i18n="mt_usb_baud_label">Швидкість</span>
          <select id="mt-serial-baud">
            <option value="115200">115200</option>
            <option value="9600">9600</option>
          </select>
        </label>
        <label class="modal-row">
          <span data-i18n="mt_bank_label">Банк</span>
          <select id="mt-serial-kind">
            <option value="privat_pos">ПриватБанк — JSON (COM)</option>
          </select>
        </label>
        <label class="modal-row">
          <span data-i18n="mt_nickname_label">Псевдонім (опційно)</span>
          <input id="mt-serial-nick" type="text" data-i18n-ph="mt_nickname_ph" placeholder="Каса USB" />
        </label>
        <div class="modal-actions">
          <button class="btn btn-default" data-i18n="btn_cancel" onclick="closeManualTerminalModal()">Скасувати</button>
          <button id="mt-serial-save" class="btn btn-update" data-i18n="btn_mt_save" onclick="saveSerialTerminal()">Додати</button>
        </div>
      </div>
    </div>
  </div>

  <div id="manual-printer-modal" class="modal-backdrop" style="display:none;">
    <div class="modal">
      <h2 data-i18n="mp_title">Додати фіскальний принтер вручну</h2>
      <p class="modal-desc" data-i18n="mp_desc">
        Італійський Epson RT-принтер (Fiscal ePOS-Print через HTTP) не
        знаходиться автопошуком. Додайте його за IP та портом fpmate
        (зазвичай 80; емулятор — 8095).
      </p>
      <label class="modal-row">
        <span data-i18n="mp_host_label">IP / Host</span>
        <input id="mp-host" type="text" data-i18n-ph="mp_host_ph" placeholder="192.168.1.120" />
      </label>
      <label class="modal-row">
        <span data-i18n="mp_port_label">Port</span>
        <input id="mp-port" type="number" value="80" />
      </label>
      <label class="modal-row">
        <span data-i18n="mp_nickname_label">Псевдонім (опційно)</span>
        <input id="mp-nickname" type="text" data-i18n-ph="mp_nickname_ph" placeholder="RT принтер" />
      </label>
      <div class="modal-actions">
        <button class="btn btn-default" data-i18n="btn_cancel" onclick="closeManualPrinterModal()">Скасувати</button>
        <button id="mp-save" class="btn btn-update" data-i18n="btn_mp_save" onclick="saveManualPrinter()">Додати</button>
      </div>
    </div>
  </div>

  <div id="uplink-modal" class="modal-backdrop" style="display:none;">
    <div class="modal">
      <h2 data-i18n="uplink_title">Віддалена діагностика</h2>
      <p class="modal-desc">
        <span data-i18n="uplink_desc1">Стрімить логи менеджера на</span>
        <code>manager.barhandler.com</code>
        <span data-i18n="uplink_desc2">і дозволяє підтримці запускати безпечні діагностичні команди
        (USB-сканування, перевірка мережі, перевірка терміналу,
        тест підключення). Без чутливих даних — api-key редактиться.</span>
      </p>
      <label class="modal-row">
        <span data-i18n="uplink_status_label">Статус підключення</span>
        <span id="uplink-status" class="muted">—</span>
      </label>
      <label class="modal-row" id="uplink-detected-row">
        <span data-i18n="uplink_detected_label">Виявлений клієнт</span>
        <span id="uplink-detected" class="muted">—</span>
      </label>
      <p id="uplink-hint" class="modal-desc" data-i18n="uplink_hint" style="margin-top:8px;">
        Щоб активувати з'єднання, відкрий свій POS-додаток
        (BarHandler / FitStudio / PetsHandler) у браузері — менеджер
        автоматично визначить tenant з домену клієнта.
      </p>
      <div class="modal-actions">
        <button class="btn btn-default" data-i18n="btn_close" onclick="closeUplinkModal()">Закрити</button>
        <button id="uplink-disable" class="btn btn-default" data-i18n="uplink_disable" onclick="saveUplink(false)" style="display:none;">Вимкнути</button>
        <button id="uplink-enable" class="btn btn-update" data-i18n="uplink_enable" onclick="saveUplink(true)">Підключити</button>
      </div>
    </div>
  </div>

  <section id="logs-panel" style="display:none;">
    <h2>
      <span data-i18n="logs_title">Логи</span>
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

  <section id="found-printers-panel" style="display:none; border-color: var(--blue);">
    <h2>
      <span data-i18n="found_printers_title">Знайдені принтери — оберіть і зареєструйте</span>
      <button class="btn btn-default" onclick="hideFound('printers')">✕</button>
    </h2>
    <table>
      <thead>
        <tr><th data-i18n="th_name">Назва</th><th data-i18n="th_transport">Transport</th><th data-i18n="fp_role">Роль</th><th data-i18n="fp_paper">Папір</th><th data-i18n="th_actions">Дії</th></tr>
      </thead>
      <tbody id="found-printers"></tbody>
    </table>
  </section>

  <section id="found-terminals-panel" style="display:none; border-color: var(--blue);">
    <h2>
      <span data-i18n="found_terminals_title">Знайдені термінали — оберіть і зареєструйте</span>
      <button class="btn btn-default" onclick="hideFound('terminals')">✕</button>
    </h2>
    <table>
      <thead>
        <tr><th data-i18n="th_name">Назва</th><th data-i18n="th_address">Адреса</th><th data-i18n="th_bank">Банк</th><th data-i18n="ft_nickname">Псевдонім</th><th data-i18n="th_actions">Дії</th></tr>
      </thead>
      <tbody id="found-terminals"></tbody>
    </table>
  </section>

  <section>
    <h2>
      <span data-i18n="printers_title">Принтери</span>
    </h2>
    <table>
      <thead>
        <tr><th data-i18n="th_id">ID</th><th data-i18n="th_name">Назва</th><th data-i18n="th_role">Роль</th><th data-i18n="th_transport">Transport</th><th data-i18n="th_state">Стан</th><th data-i18n="th_actions">Дії</th></tr>
      </thead>
      <tbody id="printers"><tr><td class="empty" colspan="6" data-i18n="loading">завантаження…</td></tr></tbody>
    </table>
  </section>

  <section>
    <h2><span data-i18n="terminals_title">POS-термінали</span></h2>
    <table>
      <thead>
        <tr><th data-i18n="th_id">ID</th><th data-i18n="th_name">Назва</th><th data-i18n="th_bank">Банк</th><th data-i18n="th_address">Адреса</th><th data-i18n="th_default_merchant">Default merchant</th><th data-i18n="th_actions">Дії</th></tr>
      </thead>
      <tbody id="terminals"><tr><td class="empty" colspan="6" data-i18n="loading">завантаження…</td></tr></tbody>
    </table>
  </section>

  <footer data-i18n="footer">Polling /health, /devices, /terminal — кожні 2 секунди.</footer>
</div>

<div id="toast" class="toast"></div>

<script>
  const API_KEY = "__API_KEY__";
  const HEADERS = { "X-Api-Key": API_KEY, "Content-Type": "application/json" };
  const GITHUB_REPO = "goodpesik/barhandler-manager";

  const $ = (id) => document.getElementById(id);
  let currentVersion = null;
  let versionCheckTimer = null;

  // Bank kind → default port, and the kind list shown when registering a
  // discovered terminal. Kept in sync with TerminalKind / _ADAPTER_FOR_KIND.
  const KIND_PORT = {
    mono_pos: 3000, generic_ssi: 3000, privat_pos: 2000,
    raif_pos: 8080, pumb_pos: 8080, generic_posapi: 8080,
    pivdenny_pos: 8888, sense_pos: 8888, generic_bpos: 8888,
    oschad_pos: 7777,
  };
  const BANK_KINDS = [
    ["mono_pos", "Monobank — SSI"], ["generic_ssi", "Інший SSI / Other SSI"],
    ["privat_pos", "ПриватБанк — JSON"],
    ["raif_pos", "Райффайзен — PosAPI"], ["pumb_pos", "ПУМБ — PosAPI"],
    ["generic_posapi", "Інший PosAPI"],
    ["pivdenny_pos", "Південний — BPOS1"], ["sense_pos", "Sense / Альфа — BPOS"],
    ["generic_bpos", "Інший BPOS"],
    ["oschad_pos", "Ощадбанк — ECR"],
  ];
  function mtKindChanged() {
    const k = $("mt-kind").value;
    if (KIND_PORT[k]) $("mt-port").value = KIND_PORT[k];
  }

  // ---- i18n ----------------------------------------------------------------

  const I18N = {
    uk: {
      version_label: "версія",
      updated_label: "оновлено",
      btn_update: "⬆ Оновити",
      controls_actions: "Дії",
      btn_refresh_status: "↻ Оновити статус",
      btn_scan_printers: "🔍 Сканувати принтери",
      btn_scan_terminals: "🔍 Сканувати термінали",
      btn_add_manual: "➕ Додати термінал вручну",
      btn_logs: "📋 Логи",
      btn_usb: "🔌 USB діагностика",
      btn_uplink: "📡 Віддалена діагностика",
      uplink_badge_connected: "● підключено",
      mt_title: "Додати термінал вручну",
      mt_desc: "Резерв на випадок коли скан не побачив (CGNAT мобільного оператора, hotspot планшета з нестандартним subnet, окрема VLAN). IP терміналу дивись у його admin меню.",
      mt_host_label: "IP / Host",
      mt_host_ph: "10.245.122.201",
      mt_port_label: "Port",
      mt_bank_label: "Банк",
      mt_tab_network: "🌐 Мережа",
      mt_tab_usb: "🔌 USB",
      mt_usb_desc: "USB-термінал (ПриватБанк) на Windows — це віртуальний COM-порт. Натисни «Пошук», обери порт і зареєструй. Термінал має бути в режимі ПК-USB-ECR (увімкнути через підтримку ПриватБанку).",
      mt_usb_scan: "🔍 Пошук USB-терміналів",
      mt_usb_port_label: "COM-порт",
      mt_usb_baud_label: "Швидкість",
      mt_usb_no_ports: "— натисни «Пошук» —",
      mt_usb_none_found: "COM-портів не знайдено (постав драйвер / не Windows)",
      mt_usb_found: "Знайдено портів: {n}",
      mt_usb_pick_port: "Обери COM-порт",
      mt_opt_mono: "Monobank (SSI)",
      mt_opt_privat: "ПриватБанк (PB)",
      mt_opt_raif: "Райффайзен (SSI)",
      mt_opt_pivdenny: "Південний (SSI)",
      mt_opt_generic: "Інший SSI",
      mt_nickname_label: "Псевдонім (опційно)",
      mt_nickname_ph: "Бар / Каса 1",
      btn_cancel: "Скасувати",
      btn_mt_save: "Додати",
      btn_add_printer_manual: "➕ Додати фіскальний принтер",
      mp_title: "Додати фіскальний принтер вручну",
      mp_desc: "Італійський Epson RT-принтер (Fiscal ePOS-Print через HTTP) не знаходиться автопошуком. Додайте його за IP та портом fpmate (зазвичай 80; емулятор — 8095).",
      mp_host_label: "IP / Host",
      mp_host_ph: "192.168.1.120",
      mp_port_label: "Port",
      mp_nickname_label: "Псевдонім (опційно)",
      mp_nickname_ph: "RT принтер",
      btn_mp_save: "Додати",
      toast_printer_added: "Принтер додано",
      uplink_title: "Віддалена діагностика",
      uplink_desc1: "Стрімить логи менеджера на",
      uplink_desc2: "і дозволяє підтримці запускати безпечні діагностичні команди (USB-сканування, перевірка мережі, перевірка терміналу, тест підключення). Без чутливих даних — api-key редактиться.",
      uplink_status_label: "Статус підключення",
      uplink_detected_label: "Виявлений клієнт",
      uplink_hint: "Щоб активувати з'єднання, відкрий свій POS-додаток (BarHandler / FitStudio / PetsHandler) у браузері — менеджер автоматично визначить tenant з домену клієнта.",
      btn_close: "Закрити",
      uplink_disable: "Вимкнути",
      uplink_enable: "Підключити",
      logs_title: "Логи",
      printers_title: "Принтери",
      terminals_title: "POS-термінали",
      th_id: "ID",
      th_name: "Назва",
      th_role: "Роль",
      th_transport: "Transport",
      th_state: "Стан",
      th_actions: "Дії",
      th_bank: "Банк",
      th_address: "Адреса",
      th_default_merchant: "Default merchant",
      loading: "завантаження…",
      footer: "Polling /health, /devices, /terminal — кожні 2 секунди.",
      btn_remove: "🗑 Видалити",
      not_registered: "— не зареєстровано —",
      confirm_remove_printer: "Видалити цей принтер із зареєстрованих?",
      confirm_remove_terminal: "Видалити цей термінал із зареєстрованих?",
      toast_printer_removed: "Принтер видалено",
      toast_terminal_removed: "Термінал видалено",
      toast_remove_failed: "Не вдалося видалити: {err}",
      toast_status_refreshed: "Статус оновлено",
      update_available: "Доступна нова версія v{latest} (зараз v{current})",
      toast_found_printers: "Знайдено принтерів: {n}",
      toast_found_terminals: "Знайдено терміналів: {n}",
      toast_scan_error: "Помилка сканування: {err}",
      found_printers_title: "Знайдені принтери — оберіть роль і зареєструйте",
      found_terminals_title: "Знайдені термінали — оберіть банк і зареєструйте",
      fp_role: "Роль",
      fp_paper: "Папір",
      ft_nickname: "Псевдонім",
      btn_register: "✓ Зареєструвати",
      btn_registering: "Реєструємо…",
      role_receipt: "Чек",
      role_kitchen: "Кухня",
      role_label: "Етикетка",
      toast_registered: "Зареєстровано ✓ — синхронізується з застосунком",
      toast_register_failed: "Не вдалося зареєструвати: {err}",
      found_nothing: "Нічого не знайдено",
      log_loading: "Завантаження…",
      log_not_exists: "({path} ще не існує)",
      log_empty: "(порожньо)",
      error_prefix: "Помилка: {err}",
      usb_probe_start: "Запуск USB діагностики…",
      toast_enter_host: "Введи IP/host",
      btn_adding: "Додаємо…",
      toast_terminal_added: "Термінал додано",
      btn_starting: "Запускаємо…",
      update_started: "Оновлення запущено!",
      btn_restarting: "Перезапуск…",
      update_error: "Помилка оновлення: {err}",
      update_no_recover: "Менеджер не піднявся після оновлення. Останній лог:",
      update_success: "Оновлено до v{cur} ✓",
      update_not_applied: "Оновлення не застосувалось — версія не змінилась (v{cur}). Лог:",
      update_still_going: "Оновлення ще йде, останні рядки логу:",
      update_timeout: "Оновлення не завершилось за {s}с. Лог:",
      uplink_connected: "підключено",
      uplink_disabled_socket: "вимкнено сокетом",
      uplink_disabled: "вимкнено",
      uplink_not_detected: "не виявлено",
      toast_saved: "Збережено",
      uplink_no_client: "Не виявлено клієнта — спочатку відкрий POS-додаток",
      uplink_error: "помилка: {err}",
      manager_unavailable: "Менеджер недоступний: {err}",
    },
    en: {
      version_label: "version",
      updated_label: "updated",
      btn_update: "⬆ Update",
      controls_actions: "Actions",
      btn_refresh_status: "↻ Refresh status",
      btn_scan_printers: "🔍 Scan printers",
      btn_scan_terminals: "🔍 Scan terminals",
      btn_add_manual: "➕ Add terminal manually",
      btn_logs: "📋 Logs",
      btn_usb: "🔌 USB diagnostics",
      btn_uplink: "📡 Remote diagnostics",
      uplink_badge_connected: "● connected",
      mt_title: "Add terminal manually",
      mt_desc: "A fallback for when the scan didn't find it (mobile operator CGNAT, a tablet hotspot on a non-standard subnet, a separate VLAN). Find the terminal's IP in its admin menu.",
      mt_host_label: "IP / Host",
      mt_host_ph: "10.245.122.201",
      mt_port_label: "Port",
      mt_bank_label: "Bank",
      mt_tab_network: "🌐 Network",
      mt_tab_usb: "🔌 USB",
      mt_usb_desc: "A USB terminal (PrivatBank) on Windows is a virtual COM port. Click Scan, pick the port and register. The terminal must be in PC-USB-ECR mode (enable it via PrivatBank support).",
      mt_usb_scan: "🔍 Scan USB terminals",
      mt_usb_port_label: "COM port",
      mt_usb_baud_label: "Baud",
      mt_usb_no_ports: "— click Scan —",
      mt_usb_none_found: "No COM ports found (install the driver / not Windows)",
      mt_usb_found: "Ports found: {n}",
      mt_usb_pick_port: "Pick a COM port",
      mt_opt_mono: "Monobank (SSI)",
      mt_opt_privat: "PrivatBank (PB)",
      mt_opt_raif: "Raiffeisen (SSI)",
      mt_opt_pivdenny: "Pivdenny (SSI)",
      mt_opt_generic: "Other SSI",
      mt_nickname_label: "Nickname (optional)",
      mt_nickname_ph: "Bar / Register 1",
      btn_cancel: "Cancel",
      btn_mt_save: "Add",
      btn_add_printer_manual: "➕ Add fiscal printer",
      mp_title: "Add a fiscal printer manually",
      mp_desc: "An Italian Epson RT printer (Fiscal ePOS-Print over HTTP) isn't found by discovery. Add it by IP and fpmate port (usually 80; emulator — 8095).",
      mp_host_label: "IP / Host",
      mp_host_ph: "192.168.1.120",
      mp_port_label: "Port",
      mp_nickname_label: "Nickname (optional)",
      mp_nickname_ph: "RT printer",
      btn_mp_save: "Add",
      toast_printer_added: "Printer added",
      uplink_title: "Remote diagnostics",
      uplink_desc1: "Streams the manager's logs to",
      uplink_desc2: "and lets support run safe diagnostic commands (USB scan, network check, terminal check, connection test). No sensitive data — the api-key is redacted.",
      uplink_status_label: "Connection status",
      uplink_detected_label: "Detected client",
      uplink_hint: "To activate the connection, open your POS app (BarHandler / FitStudio / PetsHandler) in a browser — the manager will detect the tenant from the client domain automatically.",
      btn_close: "Close",
      uplink_disable: "Disable",
      uplink_enable: "Connect",
      logs_title: "Logs",
      printers_title: "Printers",
      terminals_title: "POS terminals",
      th_id: "ID",
      th_name: "Name",
      th_role: "Role",
      th_transport: "Transport",
      th_state: "State",
      th_actions: "Actions",
      th_bank: "Bank",
      th_address: "Address",
      th_default_merchant: "Default merchant",
      loading: "loading…",
      footer: "Polling /health, /devices, /terminal — every 2 seconds.",
      btn_remove: "🗑 Remove",
      not_registered: "— none registered —",
      confirm_remove_printer: "Remove this printer from the registered ones?",
      confirm_remove_terminal: "Remove this terminal from the registered ones?",
      toast_printer_removed: "Printer removed",
      toast_terminal_removed: "Terminal removed",
      toast_remove_failed: "Failed to remove: {err}",
      toast_status_refreshed: "Status refreshed",
      update_available: "A new version v{latest} is available (currently v{current})",
      toast_found_printers: "Printers found: {n}",
      toast_found_terminals: "Terminals found: {n}",
      toast_scan_error: "Scan error: {err}",
      found_printers_title: "Found printers — pick a role and register",
      found_terminals_title: "Found terminals — pick a bank and register",
      fp_role: "Role",
      fp_paper: "Paper",
      ft_nickname: "Nickname",
      btn_register: "✓ Register",
      btn_registering: "Registering…",
      role_receipt: "Receipt",
      role_kitchen: "Kitchen",
      role_label: "Label",
      toast_registered: "Registered ✓ — will sync to the app",
      toast_register_failed: "Failed to register: {err}",
      found_nothing: "Nothing found",
      log_loading: "Loading…",
      log_not_exists: "({path} does not exist yet)",
      log_empty: "(empty)",
      error_prefix: "Error: {err}",
      usb_probe_start: "Running USB diagnostics…",
      toast_enter_host: "Enter IP/host",
      btn_adding: "Adding…",
      toast_terminal_added: "Terminal added",
      btn_starting: "Starting…",
      update_started: "Update started!",
      btn_restarting: "Restarting…",
      update_error: "Update error: {err}",
      update_no_recover: "The manager didn't come back up after the update. Last log:",
      update_success: "Updated to v{cur} ✓",
      update_not_applied: "The update wasn't applied — the version didn't change (v{cur}). Log:",
      update_still_going: "Update still in progress, last log lines:",
      update_timeout: "The update didn't finish within {s}s. Log:",
      uplink_connected: "connected",
      uplink_disabled_socket: "disabled by socket",
      uplink_disabled: "disabled",
      uplink_not_detected: "not detected",
      toast_saved: "Saved",
      uplink_no_client: "No client detected — open the POS app first",
      uplink_error: "error: {err}",
      manager_unavailable: "Manager unavailable: {err}",
    },
  };

  let LANG = localStorage.getItem("bhm:lang") || "uk";
  if (LANG !== "uk" && LANG !== "en") LANG = "uk";

  function t(key, vars) {
    const dict = I18N[LANG] || I18N.uk;
    let s = dict[key] !== undefined
      ? dict[key]
      : (I18N.uk[key] !== undefined ? I18N.uk[key] : key);
    if (vars) {
      for (const k in vars) {
        s = s.split("{" + k + "}").join(String(vars[k]));
      }
    }
    return s;
  }

  function localeTag() {
    return LANG === "en" ? "en-GB" : "uk-UA";
  }

  function applyI18n() {
    document.documentElement.lang = LANG;
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-ph")));
    });
    $("lang-uk").classList.toggle("active", LANG === "uk");
    $("lang-en").classList.toggle("active", LANG === "en");
  }

  function setLang(lang) {
    if (lang !== "uk" && lang !== "en") lang = "uk";
    LANG = lang;
    localStorage.setItem("bhm:lang", lang);
    applyI18n();
    // Re-render dynamic sections that build their text via t().
    refresh();
  }

  // ---- fetch helpers -------------------------------------------------------

  async function api(method, path, withAuth, body) {
    const opts = { method };
    if (withAuth) opts.headers = HEADERS;
    if (body !== undefined) {
      opts.headers = opts.headers || { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
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
        + "<td><button class='btn btn-danger' onclick=\"removePrinter('" + escHtml(d.id) + "')\">" + escHtml(t("btn_remove")) + "</button></td>"
        + "</tr>";
    });
    $("printers").innerHTML = rows.length
      ? rows.join("")
      : '<tr><td class="empty" colspan="6">' + escHtml(t("not_registered")) + '</td></tr>';
  }

  function renderTerminals(terminals) {
    const rows = (terminals?.terminals || []).map((reg) => {
      const d = reg.descriptor || {};
      const net = d.network || {};
      const addr = net.host
        ? escHtml(net.host) + ":" + escHtml(net.port ?? "?")
        : (d.com && d.com.port ? escHtml(d.com.port) + " (USB)" : escHtml(d.transport || ""));
      return "<tr>"
        + "<td class='id'>" + escHtml((d.id || "").slice(0, 12)) + "</td>"
        + "<td>" + escHtml(reg.nickname || d.label || "") + "</td>"
        + "<td>" + escHtml(reg.kind || "") + "</td>"
        + "<td class='id'>" + addr + "</td>"
        + "<td>" + escHtml(reg.default_merchant_id || "—") + "</td>"
        + "<td><button class='btn btn-danger' onclick=\"removeTerminal('" + escHtml(d.id) + "')\">" + escHtml(t("btn_remove")) + "</button></td>"
        + "</tr>";
    });
    $("terminals").innerHTML = rows.length
      ? rows.join("")
      : '<tr><td class="empty" colspan="6">' + escHtml(t("not_registered")) + '</td></tr>';
  }

  // ---- unregister + manual refresh ----------------------------------------

  async function removePrinter(id) {
    if (!id || !confirm(t("confirm_remove_printer"))) return;
    try {
      await api("DELETE", "/devices/" + encodeURIComponent(id), true);
      showToast(t("toast_printer_removed"), "ok");
      refresh();
    } catch (e) {
      showToast(t("toast_remove_failed", { err: e.message }), "err");
    }
  }

  async function removeTerminal(id) {
    if (!id || !confirm(t("confirm_remove_terminal"))) return;
    try {
      await api("DELETE", "/terminal/" + encodeURIComponent(id), true);
      showToast(t("toast_terminal_removed"), "ok");
      refresh();
    } catch (e) {
      showToast(t("toast_remove_failed", { err: e.message }), "err");
    }
  }

  async function refreshStatus(btn) {
    if (btn) { btn.disabled = true; }
    try {
      await refresh();
      showToast(t("toast_status_refreshed"), "ok", 1500);
    } finally {
      if (btn) { btn.disabled = false; }
    }
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
          t("update_available", { latest: latest, current: currentVersion });
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
      const found = res.printers || [];
      renderFoundPrinters(found);
      showToast(t("toast_found_printers", { n: found.length }), found.length ? "ok" : "err");
      await refresh();
    } catch (e) {
      showToast(t("toast_scan_error", { err: e.message }), "err");
    } finally {
      btn.disabled = false;
      btn.textContent = t("btn_scan_printers");
    }
  }

  // ---- discovery results (found, not yet registered) -----------------------

  function hideFound(which) {
    $("found-" + which + "-panel").style.display = "none";
  }

  function renderFoundPrinters(list) {
    const tb = $("found-printers");
    if (!list.length) { $("found-printers-panel").style.display = "none"; return; }
    tb.innerHTML = list.map((d) => {
      const id = escHtml(d.id);
      const roleOpts = [["receipt", t("role_receipt")], ["kitchen", t("role_kitchen")], ["label", t("role_label")]]
        .map(([v, l]) => "<option value='" + v + "'>" + escHtml(l) + "</option>").join("");
      return "<tr data-id='" + id + "'>"
        + "<td>" + escHtml(d.label || d.product || d.id) + "</td>"
        + "<td>" + escHtml(d.transport || "") + "</td>"
        + "<td><select class='fp-role'>" + roleOpts + "</select></td>"
        + "<td><select class='fp-paper'><option value='58'>58</option><option value='80'>80</option></select></td>"
        + "<td><button class='btn btn-update' onclick=\"registerFoundPrinter(this)\">" + escHtml(t("btn_register")) + "</button></td>"
        + "</tr>";
    }).join("");
    $("found-printers-panel").style.display = "";
  }

  async function registerFoundPrinter(btn) {
    const tr = btn.closest("tr");
    const id = tr.getAttribute("data-id");
    const kind = tr.querySelector(".fp-role").value;
    const paper_width = Number(tr.querySelector(".fp-paper").value) || 58;
    btn.disabled = true;
    btn.textContent = t("btn_registering");
    try {
      await api("POST", "/devices/register", true, { id, kind, paper_width });
      showToast(t("toast_registered"), "ok");
      tr.remove();
      if (!$("found-printers").children.length) $("found-printers-panel").style.display = "none";
      await refresh();
    } catch (e) {
      showToast(t("toast_register_failed", { err: e.message }), "err");
      btn.disabled = false;
      btn.textContent = t("btn_register");
    }
  }

  function renderFoundTerminals(list) {
    const tb = $("found-terminals");
    if (!list.length) { $("found-terminals-panel").style.display = "none"; return; }
    tb.innerHTML = list.map((d) => {
      const id = escHtml(d.id);
      const net = d.network || {};
      const addr = net.host
        ? escHtml(net.host) + ":" + escHtml(net.port ?? "?")
        : (d.com && d.com.port ? escHtml(d.com.port) + " (USB)" : escHtml(d.transport || ""));
      const opts = BANK_KINDS.map(([v, l]) =>
        "<option value='" + v + "'" + (v === d.kind ? " selected" : "") + ">" + escHtml(l) + "</option>").join("");
      return "<tr data-id='" + id + "'>"
        + "<td>" + escHtml(d.label || d.model || d.id) + "</td>"
        + "<td class='id'>" + addr + "</td>"
        + "<td><select class='ft-kind'>" + opts + "</select></td>"
        + "<td><input class='ft-nick' type='text' placeholder='" + escHtml(t("ft_nickname")) + "' style='padding:4px 8px; border-radius:6px; border:1px solid var(--border); background:var(--bg); color:var(--text); width:120px;' /></td>"
        + "<td><button class='btn btn-update' onclick=\"registerFoundTerminal(this)\">" + escHtml(t("btn_register")) + "</button></td>"
        + "</tr>";
    }).join("");
    $("found-terminals-panel").style.display = "";
  }

  async function registerFoundTerminal(btn) {
    const tr = btn.closest("tr");
    const id = tr.getAttribute("data-id");
    const kind = tr.querySelector(".ft-kind").value;
    const nickname = tr.querySelector(".ft-nick").value.trim() || null;
    btn.disabled = true;
    btn.textContent = t("btn_registering");
    try {
      await api("POST", "/terminal/register", true, { id, kind, nickname });
      showToast(t("toast_registered"), "ok");
      tr.remove();
      if (!$("found-terminals").children.length) $("found-terminals-panel").style.display = "none";
      await refresh();
    } catch (e) {
      showToast(t("toast_register_failed", { err: e.message }), "err");
      btn.disabled = false;
      btn.textContent = t("btn_register");
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
    $("log-content").textContent = t("log_loading");
    try {
      const res = await api("GET", "/system/logs?source=" + source + "&tail=500", true);
      if (!res.exists) {
        $("log-content").textContent = t("log_not_exists", { path: res.path });
        return;
      }
      const lines = res.lines || [];
      $("log-content").textContent = lines.length
        ? lines.join("\n")
        : t("log_empty");
      const el = $("log-content");
      el.scrollTop = el.scrollHeight;
    } catch (e) {
      $("log-content").textContent = t("error_prefix", { err: e.message || e });
    }
  }

  function refreshLog() {
    loadLog(currentLog);
  }

  async function runUsbProbe() {
    $("logs-panel").style.display = "block";
    $("log-content").textContent = t("usb_probe_start");
    try {
      const res = await api("POST", "/system/usb-probe", true);
      const out = res.stdout || "";
      const err = res.stderr || "";
      $("log-content").textContent =
        out + (err ? "\n--- stderr ---\n" + err : "");
    } catch (e) {
      $("log-content").textContent = t("error_prefix", { err: e.message || e });
    }
  }

  async function scanTerminals(btn) {
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const res = await api("POST", "/terminal/discover", true);
      const found = res.terminals || [];
      renderFoundTerminals(found);
      showToast(t("toast_found_terminals", { n: found.length }), found.length ? "ok" : "err");
      await refresh();
    } catch (e) {
      showToast(t("toast_scan_error", { err: e.message }), "err");
    } finally {
      btn.disabled = false;
      btn.textContent = t("btn_scan_terminals");
    }
  }

  // ---- manual terminal modal ----------------------------------------------

  function openManualTerminalModal() {
    $("manual-terminal-modal").style.display = "flex";
    $("mt-host").value = "";
    $("mt-port").value = "3000";
    $("mt-kind").value = "mono_pos";
    $("mt-nickname").value = "";
    mtTab("network");
  }

  function closeManualTerminalModal() {
    $("manual-terminal-modal").style.display = "none";
  }

  function mtTab(name) {
    const net = name === "network";
    $("mt-tab-network").style.display = net ? "" : "none";
    $("mt-tab-usb").style.display = net ? "none" : "";
    $("mt-tabbtn-network").classList.toggle("btn-update", net);
    $("mt-tabbtn-usb").classList.toggle("btn-update", !net);
  }

  // ---- USB (serial/COM) terminal — PrivatBank over USB ---------------------

  async function scanSerialPorts(btn) {
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const res = await api("POST", "/terminal/serial-scan", true);
      const ports = res.ports || [];
      const sel = $("mt-serial-port");
      if (!ports.length) {
        sel.innerHTML = '<option value="">' + escHtml(t("mt_usb_none_found")) + "</option>";
        showToast(t("mt_usb_none_found"), "err");
      } else {
        sel.innerHTML = ports.map((p) => {
          const port = (p.com && p.com.port) || "";
          const label = p.label || port;
          return '<option value="' + escHtml(port) + '">' + escHtml(label) + "</option>";
        }).join("");
        showToast(t("mt_usb_found", { n: ports.length }), "ok");
      }
    } catch (e) {
      showToast(t("toast_scan_error", { err: e.message }), "err");
    } finally {
      btn.disabled = false;
      btn.textContent = t("mt_usb_scan");
    }
  }

  async function saveSerialTerminal() {
    const port = $("mt-serial-port").value;
    const baudrate = Number($("mt-serial-baud").value) || 115200;
    const kind = $("mt-serial-kind").value;
    const nickname = $("mt-serial-nick").value.trim() || null;
    if (!port) {
      showToast(t("mt_usb_pick_port"), "err");
      return;
    }
    const btn = $("mt-serial-save");
    btn.disabled = true;
    btn.textContent = t("btn_adding");
    try {
      await api("POST", "/terminal/register-serial", true, {
        port, baudrate, kind, nickname,
      });
      showToast(t("toast_terminal_added"), "ok");
      closeManualTerminalModal();
      await refresh();
    } catch (e) {
      showToast(t("error_prefix", { err: e.message || e }), "err");
    } finally {
      btn.disabled = false;
      btn.textContent = t("btn_mt_save");
    }
  }

  async function saveManualTerminal() {
    const host = $("mt-host").value.trim();
    const port = Number($("mt-port").value) || 3000;
    const kind = $("mt-kind").value;
    const nickname = $("mt-nickname").value.trim() || null;
    if (!host) {
      showToast(t("toast_enter_host"), "err");
      return;
    }
    const btn = $("mt-save");
    btn.disabled = true;
    btn.textContent = t("btn_adding");
    try {
      await api("POST", "/terminal/register-manual", true, {
        host, port, kind, nickname,
      });
      showToast(t("toast_terminal_added"), "ok");
      closeManualTerminalModal();
      await refresh();
    } catch (e) {
      showToast(t("error_prefix", { err: e.message || e }), "err");
    } finally {
      btn.disabled = false;
      btn.textContent = t("btn_mt_save");
    }
  }

  // ---- manual fiscal printer (Italy RT / fpmate) ---------------------------

  function openManualPrinterModal() {
    $("manual-printer-modal").style.display = "flex";
    $("mp-host").value = "";
    $("mp-port").value = "80";
    $("mp-nickname").value = "";
  }

  function closeManualPrinterModal() {
    $("manual-printer-modal").style.display = "none";
  }

  async function saveManualPrinter() {
    const host = $("mp-host").value.trim();
    const port = Number($("mp-port").value) || 80;
    const nickname = $("mp-nickname").value.trim() || null;
    if (!host) {
      showToast(t("toast_enter_host"), "err");
      return;
    }
    const btn = $("mp-save");
    btn.disabled = true;
    btn.textContent = t("btn_adding");
    try {
      await api("POST", "/devices/register-manual", true, {
        host, port, kind: "fiscal_it", nickname, paper_width: 80,
      });
      showToast(t("toast_printer_added"), "ok");
      closeManualPrinterModal();
      await refresh();
    } catch (e) {
      showToast(t("error_prefix", { err: e.message || e }), "err");
    } finally {
      btn.disabled = false;
      btn.textContent = t("btn_mp_save");
    }
  }

  // ---- update --------------------------------------------------------------

  async function doUpdate() {
    const btn = $("btn-update");
    btn.disabled = true;
    btn.textContent = t("btn_starting");
    // Remember what we're updating FROM so we can tell a real update
    // (version changed) from a silent no-op (came back on the same
    // version — failed download, port conflict, never restarted).
    let beforeVer = null;
    try {
      const v = await api("GET", "/version", false);
      beforeVer = v.version || null;
    } catch (_) {}
    try {
      const res = await api("POST", "/system/update", true);
      showToast(res.message || t("update_started"), "ok", 10000);
      btn.textContent = t("btn_restarting");
      watchUpdate(beforeVer);
    } catch (e) {
      showToast(t("update_error", { err: e.message }), "err");
      btn.disabled = false;
      btn.textContent = t("btn_update");
    }
  }

  // Watch the update through to a verdict instead of spinning forever.
  // Outcomes:
  //   • version changed         → success, reset the button
  //   • came back same version  → no-op, surface update.log so the
  //                               operator sees WHY (download failed,
  //                               old process still holding the port…)
  //   • never recovered in time → timeout, surface update.log
  async function watchUpdate(beforeVer) {
    const POLL_MS = 3000;
    const DEADLINE_MS = 150000;       // 2.5 min — covers venv rebuilds
    const started = Date.now();
    let sawDown = false;              // did the manager actually restart?
    let progressShown = false;

    const finishFail = async (headline) => {
      let last = "";
      try {
        const log = await api("GET", "/system/update-log?tail=30", true);
        last = (log.lines || []).slice(-8).join("\n");
      } catch (_) {}
      showToast(headline + (last ? "\n\n" + last : ""), "err", 20000);
      const btn = $("btn-update");
      btn.disabled = false;
      btn.textContent = t("btn_update");
    };

    const tick = async () => {
      const elapsed = Date.now() - started;
      let cur = null;
      try {
        const v = await api("GET", "/version", false);
        cur = v.version || null;
      } catch (_) {
        // /version throws while the manager is restarting — that's the
        // expected mid-update dip, not a failure.
        sawDown = true;
        if (elapsed < DEADLINE_MS) return setTimeout(tick, POLL_MS);
        return finishFail(t("update_no_recover"));
      }
      // Reachable again.
      if (cur && beforeVer && cur !== beforeVer) {
        showToast(t("update_success", { cur: cur }), "ok", 8000);
        const btn = $("btn-update");
        btn.disabled = false;
        btn.textContent = t("btn_update");
        return;
      }
      // Still on the old version. If it already bounced and came back
      // unchanged, the update didn't take — report now. Otherwise keep
      // waiting (the restart may not have happened yet).
      if (sawDown && cur && beforeVer && cur === beforeVer) {
        return finishFail(t("update_not_applied", { cur: cur }));
      }
      if (elapsed > 60000 && !progressShown) {
        progressShown = true;
        try {
          const log = await api("GET", "/system/update-log?tail=30", true);
          const last = (log.lines || []).slice(-6).join("\n");
          showToast(t("update_still_going") + "\n" + last, "ok", 15000);
        } catch (_) {}
      }
      if (elapsed < DEADLINE_MS) return setTimeout(tick, POLL_MS);
      return finishFail(t("update_timeout", { s: Math.round(DEADLINE_MS / 1000) }));
    };
    setTimeout(tick, POLL_MS);
  }

  // ---- uplink modal --------------------------------------------------------

  async function openUplinkModal() {
    $("uplink-modal").style.display = "flex";
    try {
      const cur = await api("GET", "/system/uplink", true);
      if (cur.connected) {
        // Connected mode — minimal: status + tenant + one button to disconnect.
        $("uplink-status").innerHTML = "<span class='ok-text'>" + escHtml(t("uplink_connected")) + "</span>";
        $("uplink-detected").innerHTML = "<span class='ok-text'>" + escHtml(cur.tenant || "—") + "</span>";
        $("uplink-hint").style.display = "none";
        $("uplink-enable").style.display = "none";
        $("uplink-disable").style.display = "";
        $("uplink-disable").textContent = t("uplink_disable");
      } else {
        // Disconnected mode — allow connect if tenant detected.
        $("uplink-status").innerHTML = cur.enabled
          ? "<span class='err-text'>" + escHtml(t("uplink_disabled_socket")) + "</span>"
          : "<span class='muted'>" + escHtml(t("uplink_disabled")) + "</span>";
        const tenantToShow = cur.tenant || cur.detected_tenant;
        if (tenantToShow) {
          $("uplink-detected").innerHTML = "<span class='ok-text'>" + escHtml(tenantToShow) + "</span>";
          $("uplink-hint").style.display = "none";
          $("uplink-enable").disabled = false;
        } else {
          $("uplink-detected").innerHTML = "<span class='err-text'>" + escHtml(t("uplink_not_detected")) + "</span>";
          $("uplink-hint").style.display = "";
          $("uplink-enable").disabled = true;
        }
        $("uplink-enable").style.display = "";
        $("uplink-enable").textContent = t("uplink_enable");
        $("uplink-disable").style.display = "none";
      }
    } catch (e) {
      $("uplink-status").innerHTML = "<span class='err-text'>" + escHtml(t("uplink_error", { err: e.message })) + "</span>";
    }
  }

  function closeUplinkModal() {
    $("uplink-modal").style.display = "none";
  }

  async function saveUplink(enabled) {
    $("uplink-enable").disabled = true;
    $("uplink-disable").disabled = true;
    try {
      const res = await api("POST", "/system/uplink", true, { enabled });
      showToast(res.message || t("toast_saved"), "ok", 6000);
      closeUplinkModal();
      // Manager is about to SIGTERM itself; the next /health poll will
      // fail, the error banner will appear, then once the service
      // manager respawns it'll come back and the modal will reflect
      // the new state.
    } catch (e) {
      // FastAPI wraps the 400 in `{detail: "..."}` — pull it out for a
      // readable error toast.
      let msg = e.message || String(e);
      try {
        const m = msg.match(/→ (\d+)/);
        if (m && m[1] === "400") msg = t("uplink_no_client");
      } catch (_) {}
      showToast(t("error_prefix", { err: msg }), "err");
    } finally {
      $("uplink-enable").disabled = false;
      $("uplink-disable").disabled = false;
    }
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
      let devices, terminals, uplink;
      try { devices = await api("GET", "/devices", true); } catch (_) { devices = null; }
      try { terminals = await api("GET", "/terminal", true); } catch (_) { terminals = null; }
      try { uplink = await api("GET", "/system/uplink", true); } catch (_) { uplink = null; }
      renderPrinters(devices, health);
      renderTerminals(terminals);
      $("uplink-badge").style.display = (uplink && uplink.connected) ? "" : "none";
      $("updated").textContent = new Date().toLocaleTimeString(localeTag());
      $("status-dot").className = "dot ok";
      $("error-banner").className = "error-banner";
    } catch (e) {
      $("status-dot").className = "dot err";
      $("error-banner").className = "error-banner show";
      $("error-banner").textContent = t("manager_unavailable", { err: e.message || e });
    }
  }

  applyI18n();
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
