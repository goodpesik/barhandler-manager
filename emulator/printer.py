"""Interactive runner for the ESC/POS printer emulator.

    python -m emulator.printer                 # RAW 9100, viewer on :8089
    python -m emulator.printer --port 9100 --web-port 8089 --host 0.0.0.0

Acts as a network thermal printer: the manager prints to it over RAW/9100
and we reconstruct the exact bitmap into a **live web page** — open the
viewer URL and every receipt appears as it's "printed", newest on top.
Nothing is written to disk; receipts live in memory only.

Local test tool only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .escpos_printer import (
    PAPER_DOTS,
    PrinterState,
    Receipt,
    start_server_thread,
)

console = Console()

API_KEY = "bf11b47b-e139-4f03-8e02-9c2e692f91b8"  # manager DEFAULT_API_KEY


# ---------------------------------------------------------------------------
# Live web viewer (stdlib http.server — no extra deps)
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="uk"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BarHandler — емулятор принтера</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; background:#15171c; color:#c9d1d9;
         font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif; }
  header { position:sticky; top:0; padding:12px 18px; background:#1b1e25;
           border-bottom:1px solid #2b2f3a; display:flex; gap:16px;
           align-items:center; }
  header b { color:#58d68d; }
  #count { margin-left:auto; color:#8b949e; }
  #roll { display:flex; flex-direction:column; align-items:center;
          gap:22px; padding:26px 12px 80px; }
  .receipt { background:#fff; padding:10px 8px; border-radius:3px;
             box-shadow:0 6px 24px rgba(0,0,0,.5); image-rendering:pixelated; }
  .receipt img { display:block; }
  .meta { color:#6e7681; font-size:12px; text-align:center; margin-top:6px; }
  #empty { color:#6e7681; text-align:center; padding:60px 20px; }
  .pill { background:#2b2f3a; border-radius:10px; padding:2px 9px; color:#adbac7; }
</style></head>
<body>
<header>
  🖨️ <b>BarHandler printer emulator</b>
  <span class="pill" id="paper">—</span>
  <span id="count">очікую друк…</span>
</header>
<div id="roll"><div id="empty">Ще нічого не надруковано.<br>
Надішліть чек на принтер — він з'явиться тут.</div></div>
<script>
let known = new Set();
async function tick() {
  try {
    const r = await fetch('state'); const s = await r.json();
    document.getElementById('count').textContent =
      s.receipts.length ? (s.receipts.length + ' чек(ів)') : 'очікую друк…';
    if (s.receipts[0]) document.getElementById('paper').textContent =
      s.receipts[0].paper_mm + 'мм';
    const roll = document.getElementById('roll');
    for (const rec of s.receipts) {        // newest first
      if (known.has(rec.id)) continue;
      known.add(rec.id);
      const empty = document.getElementById('empty'); if (empty) empty.remove();
      const wrap = document.createElement('div'); wrap.className = 'receipt';
      const img = document.createElement('img');
      img.src = 'receipt/' + rec.id + '.png?w=' + rec.width;
      img.style.width = Math.round(rec.width/2) + 'px';   // 203dpi → ~CSS px
      const meta = document.createElement('div'); meta.className = 'meta';
      meta.textContent = '#' + rec.id + ' · ' + rec.paper_mm + 'мм · ' +
        rec.height + 'px · ' + new Date(rec.ts*1000).toLocaleTimeString();
      wrap.appendChild(img); wrap.appendChild(meta);
      roll.insertBefore(wrap, roll.firstChild);          // prepend = newest top
    }
  } catch (e) { /* manager/emulator restarting — keep polling */ }
}
setInterval(tick, 1000); tick();
</script>
</body></html>"""


def _make_handler(state: PrinterState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):       # silence default access logging
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                    # noqa: N802 — stdlib signature
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/state":
                receipts: list[Receipt] = state.snapshot()
                body = json.dumps({
                    "receipts": [
                        {"id": r.id, "width": r.width, "height": r.height,
                         "paper_mm": r.paper_mm, "ts": r.ts}
                        for r in receipts
                    ],
                }).encode("utf-8")
                self._send(200, body, "application/json")
            elif path.startswith("/receipt/") and path.endswith(".png"):
                try:
                    rid = int(path[len("/receipt/"):-len(".png")])
                except ValueError:
                    self._send(404, b"bad id", "text/plain")
                    return
                rec = state.get(rid)
                if rec is None:
                    self._send(404, b"gone", "text/plain")
                else:
                    self._send(200, rec.png, "image/png")
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def start_web_thread(host: str, port: int, state: PrinterState) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _make_handler(state))
    import threading
    threading.Thread(target=server.serve_forever, name="printer-web", daemon=True).start()
    return server


# ---------------------------------------------------------------------------
# Optional localhost self-registration into the manager's printers.json
# ---------------------------------------------------------------------------


def _network_id(host: str, port: int) -> str:
    """Replicate src/models/printer.make_id(network, host, port)."""
    payload = ":".join(["network", host, str(port)]).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:12]


def _self_register(path: Path, host: str, port: int, paper: int, kind: str) -> str:
    pid = _network_id(host, port)
    data = {"printers": []}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            data = {"printers": []}
    entry = {
        "descriptor": {
            "id": pid, "transport": "network",
            "label": f"Emulator {host}:{port}",
            "network": {"host": host, "port": port},
        },
        "kind": kind, "nickname": "Емулятор-принтер",
        "paper_width": paper, "render_mode": "bitmap",
        "code_page": None, "drawer_pin": 0, "protocol": "escpos",
    }
    others = [p for p in data.get("printers", [])
              if p.get("descriptor", {}).get("id") != pid]
    data["printers"] = others + [entry]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return pid


# ---------------------------------------------------------------------------
# Banner + console
# ---------------------------------------------------------------------------


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _banner(args, web_url: str, lan_ip: str) -> None:
    body = Text()
    body.append("ESC/POS thermal printer — device side\n\n", style="bold cyan")
    body.append("RAW/9100 sink   ", style="dim")
    body.append(f"{args.host}:{args.port}\n", style="bold green")
    body.append("Live viewer     ", style="dim")
    body.append(f"{web_url}\n\n", style="bold green")
    body.append("Register it in the manager — discovery scans the LAN, so\n", style="dim")
    body.append("bind to 0.0.0.0 and run discover, then register:\n", style="dim")
    body.append(
        f'  curl -s -X POST http://127.0.0.1:{args.manager_port}/devices/discover \\\n'
        f'    -H "X-Api-Key: {API_KEY}" | jq\n'
        f'  # copy the network printer\'s id, then:\n'
        f'  curl -X POST http://127.0.0.1:{args.manager_port}/devices/register \\\n'
        f'    -H "X-Api-Key: {API_KEY}" -H "Content-Type: application/json" \\\n'
        f'    -d \'{{"id":"<id>","kind":"receipt","paper_width":{args.paper}}}\'\n\n',
        style="yellow",
    )
    body.append("Localhost (no LAN)? ", style="dim")
    body.append("--register printers.json", style="bold")
    body.append(" seeds it directly (restart manager).\n", style="dim")
    console.print(Panel(body, title="🖨️  BARHANDLER PRINTER EMULATOR", border_style="cyan"))


def main() -> None:
    parser = argparse.ArgumentParser(description="ESC/POS thermal printer emulator")
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind address for the RAW/9100 sink (0.0.0.0 so "
                             "the manager's LAN discovery can find it)")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--web-host", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8089)
    parser.add_argument("--manager-port", type=int, default=9999)
    parser.add_argument("--paper", type=int, choices=(58, 80), default=58,
                        help="fallback paper width when a job sends no raster "
                             "(width is auto-detected from GS v 0 otherwise)")
    parser.add_argument("--register", metavar="PRINTERS_JSON", default=None,
                        help="seed a network-printer registration into this "
                             "printers.json (localhost flow; restart manager)")
    args = parser.parse_args()

    logging.basicConfig(
        filename="printer-emulator.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    state = PrinterState()

    def _notify(rec: Receipt) -> None:
        console.print(
            f"   [bold green]🧾 #{rec.id}[/]  {rec.paper_mm}мм · "
            f"{rec.width}×{rec.height}px"
        )

    state.on_notify(_notify)

    requested_port = args.port
    args.port = start_server_thread(args.host, args.port, state, PAPER_DOTS[args.paper])
    if args.port != requested_port:
        console.print(
            f"[yellow]⚠ RAW-порт {requested_port} зайнятий — слухаю на "
            f"{args.port}[/] (дискавер сканує 9100–9102, тож знайде обидва)."
        )
    web = start_web_thread(args.web_host, args.web_port, state)
    web_url = f"http://{args.web_host}:{web.server_port}"

    if args.register:
        pid = _self_register(Path(args.register), "127.0.0.1", args.port,
                             args.paper, "receipt")
        console.print(f"[green]Registered[/] id=[bold]{pid}[/] → {args.register} "
                      f"(restart the manager to load it)\n")

    _banner(args, web_url, _lan_ip())
    console.print("[dim]Чекаю на друк… (Ctrl-C — вихід)[/]\n")
    try:
        import time
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Вихід.[/]")


if __name__ == "__main__":
    main()
