"""Epson **Fiscal ePOS-Print** emulator — the RT fiscal-printer (Documento
Commerciale) device side, for local testing of PET-237.

Local test tool only — NOT part of the manager runtime.

Where `emulator.printer` emulates a NETWORK ESC/POS thermal printer (raw TCP on
:9100), the Italian RT fiscal printer speaks a completely different protocol: it
exposes the **EpsonFPMate** web service and receives a SOAP-wrapped *fiscal
ePOS-Print XML* document over HTTP. So this emulator is an HTTP server, not a TCP
sink: it accepts `POST /cgi-bin/fpmate.cgi`, parses the fiscal XML that
`src/services/fiscal_it.py` produces, keeps an in-memory fiscal journal
(receipt + Z counters, last-Z clock), renders each Documento Commerciale in a
live web viewer, and returns the exact `<response success=…><addInfo>…` XML the
manager's `parse_response()` expects.

It also models the RT rule that matters most: with `--require-first-z`, a sale
before the day's first Z is rejected with **error 17 (IMPOSSIBILE ORA)** — the
real printer's behaviour — so the error path can be exercised end-to-end.

Run:
    python -m emulator.fiscal_epos                       # :8095 — fpmate POST + web viewer at http://127.0.0.1:8095/
    python -m emulator.fiscal_epos --port 8095 --require-first-z
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:  # rich is in emulator/requirements.txt; degrade gracefully if missing.
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    _console: Console | None = Console()
except Exception:  # pragma: no cover
    _console = None

logger = logging.getLogger("emulator.fiscal_epos")

FPMATE_PATH = "/cgi-bin/fpmate.cgi"
# Manager's default dev API key (src/constants.py DEFAULT_API_KEY) — for the
# copy-paste register curl in the banner.
API_KEY = "bf11b47b-e139-4f03-8e02-9c2e692f91b8"


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _money_comma(value: float) -> str:
    """RT printers echo amounts with a comma decimal (1,08)."""
    return f"{value:.2f}".replace(".", ",")


# ---------------------------------------------------------------------------
# In-memory fiscal journal
# ---------------------------------------------------------------------------


class FiscalState:
    """Thread-safe in-memory fiscal journal for one emulated RT printer."""

    def __init__(self, *, require_first_z: bool = False, keep: int = 50):
        self._lock = threading.Lock()
        self._keep = keep
        self.require_first_z = require_first_z
        self.first_z_done = not require_first_z
        self.receipt_number = 0  # daily fiscal receipt counter
        self.z_number = 0  # Z-report (chiusura) counter
        self.x_number = 0  # X-report (lettura) counter
        self.last_z_at: datetime | None = None
        self.docs: list[dict] = []  # rendered documents, newest last
        self._id = 0
        self.on_notify = None  # optional callback(label:str)

    # -- helpers ----------------------------------------------------------
    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _record_doc(self, doc: dict) -> None:
        self._id += 1
        doc["id"] = self._id
        doc["ts"] = self._now().isoformat()
        self.docs.append(doc)
        if len(self.docs) > self._keep:
            self.docs = self.docs[-self._keep :]
        if self.on_notify:
            try:
                self.on_notify(f"{doc['kind']} #{doc.get('number') or doc['id']}")
            except Exception:  # pragma: no cover
                pass

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(reversed(self.docs))

    # -- fiscal operations -----------------------------------------------
    def record_sale(self, *, items: list[dict], total: str, is_refund: bool,
                    payment_type: str) -> dict:
        """Returns the addInfo `fields` dict, or raises _Rejected(code)."""
        with self._lock:
            if self.require_first_z and not self.first_z_done:
                # RT rule: the first daily Z must run before any sale.
                self._record_doc({
                    "kind": "REJECTED",
                    "reason": "error 17 — first Z not run",
                    "items": items,
                    "total": total,
                })
                raise _Rejected("17")
            self.receipt_number += 1
            now = self._now()
            self._record_doc({
                "kind": "RESO" if is_refund else "DOCUMENTO COMMERCIALE",
                "number": self.receipt_number,
                "z": self.z_number,
                "items": items,
                "total": total,
                "payment_type": payment_type,
                "is_refund": is_refund,
            })
            return {
                "fiscalReceiptNumber": str(self.receipt_number),
                "fiscalReceiptAmount": _money_comma(_to_float(total)),
                "fiscalReceiptDate": now.strftime("%d/%m/%Y"),
                "fiscalReceiptTime": now.strftime("%H:%M"),
                "zRepNumber": f"{self.z_number:04d}",
            }

    def run_z(self) -> dict:
        with self._lock:
            self.z_number += 1
            self.first_z_done = True
            self.last_z_at = self._now()
            self.receipt_number = 0  # a Z closes the day; receipts reset
            self._record_doc({"kind": "CHIUSURA (Z)", "number": self.z_number})
            return {
                "zRepNumber": f"{self.z_number:04d}",
                "fiscalReceiptDate": self.last_z_at.strftime("%d/%m/%Y"),
                "fiscalReceiptTime": self.last_z_at.strftime("%H:%M"),
            }

    def run_x(self) -> dict:
        with self._lock:
            self.x_number += 1
            self._record_doc({"kind": "LETTURA (X)", "number": self.x_number})
            return {"reportNumber": f"{self.x_number:04d}"}

    def reprint(self, receipt_number: str) -> dict:
        """Reprint a duplicate of an existing document by number — the printer
        finds it in its own journal and prints a COPIA (no re-formed receipt)."""
        with self._lock:
            orig = next(
                (
                    d
                    for d in reversed(self.docs)
                    if str(d.get("number")) == str(receipt_number)
                    and d.get("kind") in ("DOCUMENTO COMMERCIALE", "RESO")
                ),
                None,
            )
            now = self._now()
            self._record_doc({
                "kind": "COPIA",
                "number": receipt_number,
                "items": (orig or {}).get("items", []),
                "total": (orig or {}).get("total"),
                "reason": f"duplicato del n. {receipt_number}",
            })
            return {
                "fiscalReceiptNumber": str(receipt_number),
                "fiscalReceiptDate": now.strftime("%d/%m/%Y"),
                "fiscalReceiptTime": now.strftime("%H:%M"),
                "zRepNumber": f"{self.z_number:04d}",
            }

    def status(self) -> dict:
        with self._lock:
            fields: dict = {"zRepNumber": f"{self.z_number:04d}"}
            if self.last_z_at is not None:
                fields["fiscalReceiptDate"] = self.last_z_at.strftime("%d/%m/%Y")
                fields["fiscalReceiptTime"] = self.last_z_at.strftime("%H:%M")
            return fields


class _Rejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _to_float(text: str) -> float:
    try:
        return float(str(text).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Request parsing + response building
# ---------------------------------------------------------------------------


def parse_fiscal_request(body: str) -> dict:
    """Parse a (SOAP-wrapped) fiscal ePOS-Print request into a command dict.

    Returns {command: 'sale'|'z'|'x'|'status'|'unknown', items, total,
    is_refund, payment_type}.
    """
    root = ET.fromstring(body)
    by_name: dict[str, list[ET.Element]] = {}
    for el in root.iter():
        by_name.setdefault(_localname(el.tag), []).append(el)

    if by_name.get("printZReport"):
        return {"command": "z"}
    if by_name.get("printXReport"):
        return {"command": "x"}
    if by_name.get("queryPrinterStatus"):
        return {"command": "status"}
    if by_name.get("printDuplicateReceipt"):
        el = by_name["printDuplicateReceipt"][0]
        return {"command": "reprint", "receipt_number": el.get("receiptNumber", "")}
    if by_name.get("printerFiscalReceipt"):
        is_refund = bool(by_name.get("printRecRefund")) or any(
            m.get("messageType") == "4" for m in by_name.get("printRecMessage", [])
        )
        items = []
        for el in by_name.get("printRecItem", []) + by_name.get("printRecRefund", []):
            items.append({
                "description": el.get("description", ""),
                "quantity": el.get("quantity", ""),
                "unitPrice": el.get("unitPrice", ""),
                "department": el.get("department", ""),
            })
        total = ""
        payment_type = ""
        totals = by_name.get("printRecTotal", [])
        if totals:
            total = totals[-1].get("payment", "")
            payment_type = totals[-1].get("paymentType", "")
        return {
            "command": "sale",
            "items": items,
            "total": total,
            "is_refund": is_refund,
            "payment_type": payment_type,
        }
    return {"command": "unknown"}


def build_response(fields: dict, *, success: bool = True, code: str = "",
                   status: str = "12345") -> str:
    """Build the SOAP-wrapped `<response>` the manager's parser reads."""
    resp = ET.Element("response", {
        "success": "true" if success else "false",
        "code": code,
        "status": status,
    })
    add = ET.SubElement(resp, "addInfo")
    if fields:
        el_list = ET.SubElement(add, "elementList")
        el_list.text = ",".join(fields.keys())
        for k, v in fields.items():
            ET.SubElement(add, k).text = str(v)
    inner = ET.tostring(resp, encoding="unicode")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f"<s:Body>{inner}</s:Body></s:Envelope>"
    )


def handle_request(state: FiscalState, body: str) -> str:
    """Top-level dispatch used by both the HTTP handler and the tests."""
    try:
        cmd = parse_fiscal_request(body)
    except ET.ParseError as exc:
        return build_response({}, success=False, code="bad_xml", status=str(exc))

    kind = cmd["command"]
    if kind == "z":
        return build_response(state.run_z())
    if kind == "x":
        return build_response(state.run_x())
    if kind == "reprint":
        return build_response(state.reprint(cmd.get("receipt_number", "")))
    if kind == "status":
        return build_response(state.status())
    if kind == "sale":
        try:
            fields = state.record_sale(
                items=cmd["items"],
                total=cmd["total"],
                is_refund=cmd["is_refund"],
                payment_type=cmd["payment_type"],
            )
        except _Rejected as rej:
            return build_response({}, success=False, code=rej.code, status="12345")
        return build_response(fields)
    return build_response({}, success=False, code="unknown_command", status="0")


# ---------------------------------------------------------------------------
# Live web viewer
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Fiscal ePOS emulator</title>
<style>
 body{background:#0d0f12;color:#cfd3d8;font-family:ui-monospace,Menlo,monospace;margin:0;padding:20px}
 h1{font-size:14px;color:#8fd08f;margin:0 0 4px;font-family:system-ui,sans-serif}
 .hint{color:#6b7280;font-size:12px;margin:0 0 16px;font-family:system-ui,sans-serif}
 #roll{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}
 /* thermal paper */
 .r{background:#fbfbf7;color:#111;width:320px;padding:14px 16px;border-radius:3px;
    box-shadow:0 2px 8px rgba(0,0,0,.5);font-size:12.5px;line-height:1.45}
 .r.reso{outline:2px solid #a6772e}.r.copia{outline:2px dashed #888}.r.rej{outline:2px solid #c0392b}
 .c{text-align:center}.b{font-weight:700}.mut{color:#666}.rr{text-align:right}
 .biz{font-size:12px}.title{margin:8px 0 2px;font-weight:700;letter-spacing:.5px}
 .sub{font-weight:400;font-size:11px}
 .sep{border-top:1px dashed #999;margin:8px 0}
 table{width:100%;border-collapse:collapse}
 td{padding:1px 0;vertical-align:top}
 .row{display:flex;justify-content:space-between}
 .big{font-size:15px;font-weight:700}
 .foot{font-size:11px}
 .info{background:#1c1c1c;color:#cfd3d8;border-radius:6px;padding:10px 12px;width:320px;font-size:12px}
 .info.z{border-left:3px solid #3a7}.info.x{border-left:3px solid #58f}
</style></head><body>
<h1>🧾 Epson Fiscal ePOS-Print emulator</h1>
<p class="hint">Documento Commerciale journal — live. Reparto→IVA: 1=22% 3=10% 5=5% 7=4% 9=0%.</p>
<div id="roll"></div>
<script>
var IVA={1:22,3:10,5:5,7:4,9:0};
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function num(x){return parseFloat(String(x==null?0:x).replace(',','.'))||0;}
function eur(n){return (Math.round(n*100)/100).toFixed(2).replace('.',',');}
function qty(q){return (q%1)?q.toFixed(3):String(q);}
function fmtTs(ts){if(!ts)return '';var d=new Date(ts);if(isNaN(d.getTime()))return ts;
 function p(n){return String(n).padStart(2,'0');}
 return p(d.getDate())+'-'+p(d.getMonth()+1)+'-'+d.getFullYear()+'  '+p(d.getHours())+':'+p(d.getMinutes());}
function docNo(d){var az=(d.z!=null?d.z+1:1),pr=(d.number||0);
 return String(az).padStart(4,'0')+'-'+String(pr).padStart(4,'0');}
function receipt(d){
 var reso=d.kind==='RESO',copia=d.kind==='COPIA';
 var items=d.items||[],sum=0,ivaTot=0;
 var rows=items.map(function(it){
  var q=num(it.quantity)||1,lt=num(it.unitPrice)*q,pct=IVA[parseInt(it.department,10)]||0;
  sum+=lt;ivaTot+=lt*pct/(100+pct);
  return '<tr><td>'+esc(it.description)+' <span class="mut">×'+qty(q)+'</span></td>'+
         '<td class="rr mut">'+pct+'%</td><td class="rr">'+eur(lt)+'</td></tr>';
 }).join('');
 var total=(d.total!=null&&d.total!=='')?num(d.total):sum;
 var pay=(String(d.payment_type)==='0')?'Pagamento contante':
         (String(d.payment_type)==='2')?'Pagamento elettronico':'Pagamento';
 var cls='r'+(reso?' reso':'')+(copia?' copia':'');
 var title=reso?'DOCUMENTO COMMERCIALE<br><span class="sub">emesso per RESO MERCE</span>'
                :'DOCUMENTO COMMERCIALE<br><span class="sub">di vendita o prestazione</span>';
 return '<div class="'+cls+'">'+
  '<div class="c biz b">EMULATORE RT · PETSHANDLER</div>'+
  '<div class="c biz mut">Via di Prova 1 — 00100 Roma (RM)</div>'+
  '<div class="c biz mut">P.IVA 00000000000</div>'+
  (copia?'<div class="c b" style="margin-top:6px">— COPIA —</div>':'')+
  '<div class="c title">'+title+'</div>'+
  '<div class="sep"></div>'+
  '<table>'+rows+'</table>'+
  '<div class="sep"></div>'+
  '<div class="row big"><span>TOTALE COMPLESSIVO</span><span>'+eur(total)+'</span></div>'+
  '<div class="row mut"><span>di cui IVA</span><span>'+eur(ivaTot)+'</span></div>'+
  '<div class="sep"></div>'+
  '<div class="row"><span>'+pay+'</span><span>'+eur(reso?-total:total)+'</span></div>'+
  '<div class="row mut"><span>Non riscosso</span><span>0,00</span></div>'+
  '<div class="sep"></div>'+
  '<div class="c foot b">DOCUMENTO N. '+docNo(d)+'</div>'+
  '<div class="c foot mut">'+fmtTs(d.ts)+'</div>'+
  '<div class="c foot mut">RT 2CMZP999891 (EMULATORE)</div>'+
  '</div>';
}
function info(d){
 var cls=d.kind.indexOf('CHIUSURA')>=0?'info z':(d.kind.indexOf('LETTURA')>=0?'info x':'info');
 if(d.kind==='REJECTED')cls='r rej';
 var body=d.kind==='REJECTED'
   ? '<div class="c b">SCONTRINO NON EMESSO</div><div class="c mut">'+esc(d.reason||'')+'</div>'
   : '<div class="b">'+esc(d.kind)+' n. '+String(d.number||'').padStart(4,'0')+'</div>'+
     '<div class="mut">'+fmtTs(d.ts)+'</div>';
 return '<div class="'+cls+'">'+body+'</div>';
}
async function poll(){
 try{var r=await fetch('state');var docs=await r.json();
  document.getElementById('roll').innerHTML=docs.map(function(d){
   return (d.kind==='DOCUMENTO COMMERCIALE'||d.kind==='RESO'||d.kind==='COPIA')?receipt(d):info(d);
  }).join('');
 }catch(e){}
 setTimeout(poll,1000);
}
poll();
</script></body></html>"""


def _make_handler(state: FiscalState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # silence default stderr spam
            pass

        def _send(self, code: int, ctype: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") in ("", "/") or self.path.startswith("/index"):
                self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))
            elif self.path.startswith("/state"):
                body = json.dumps(state.snapshot()).encode("utf-8")
                self._send(200, "application/json", body)
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
            path = self.path.split("?", 1)[0]
            if path != FPMATE_PATH:
                self._send(404, "text/plain", b"not the fpmate endpoint")
                return
            try:
                resp = handle_request(state, body)
            except Exception as exc:  # pragma: no cover — defensive
                logger.exception("fiscal_epos: handler error")
                resp = build_response({}, success=False, code="emulator_error", status=str(exc))
            self._send(200, 'text/xml; charset=UTF-8', resp.encode("utf-8"))

    return Handler


def start_server(state: FiscalState, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------


def _banner(host: str, port: int, manager_port: int) -> None:
    url = f"http://{host}:{port}{FPMATE_PATH}"
    viewer = f"http://{host}:{port}/"
    dashboard = f"http://127.0.0.1:{manager_port}/"
    # Preferred: register from the manager dashboard → "Add fiscal printer"
    # (host + port below). The curl is only for headless/no-browser setups.
    register = (
        f"curl -X POST http://127.0.0.1:{manager_port}/devices/register-manual "
        f"-H 'X-Api-Key: {API_KEY}' -H 'Content-Type: application/json' "
        f"-d '{{\"host\":\"{host}\",\"port\":{port},\"kind\":\"fiscal_it\","
        f"\"nickname\":\"RT emulator\"}}'"
    )
    if _console is None:
        print(
            f"Fiscal ePOS emulator — FPMate {url} · viewer {viewer}\n"
            f"Register in the manager dashboard ({dashboard}) → Add fiscal printer "
            f"(host {host}, port {port}).\nHeadless fallback:\n{register}"
        )
        return
    body = Text()
    body.append("🏦 BARHANDLER — FISCAL ePOS-PRINT EMULATOR\n", style="bold")
    body.append("device side of an Italian Epson RT printer\n\n", style="dim")
    body.append("FPMate endpoint: ", style="")
    body.append(f"{url}\n", style="bold green")
    body.append("Web viewer:      ", style="")
    body.append(f"{viewer}\n\n", style="bold green")
    body.append("Register in the manager dashboard:\n", style="dim")
    body.append(f"  {dashboard}", style="bold cyan")
    body.append("  →  ", style="dim")
    body.append("Add fiscal printer", style="bold")
    body.append(f"  (host {host}, port {port})\n", style="dim")
    body.append("then bind it to the Italy fiscal card in Settings.\n", style="dim")
    body.append("\nHeadless / no browser? register via curl:\n", style="dim")
    body.append(f"{register}\n", style="yellow")
    body.append("\nЧекаю фіскальні документи…", style="")
    _console.print(Panel(body, border_style="green"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Epson Fiscal ePOS-Print emulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8095, help="FPMate HTTP port")
    parser.add_argument("--manager-port", type=int, default=9999)
    parser.add_argument(
        "--require-first-z",
        action="store_true",
        help="reject sales with error 17 until the day's first Z is run",
    )
    args = parser.parse_args()

    logging.basicConfig(
        filename="fiscal-epos-emulator.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    state = FiscalState(require_first_z=args.require_first_z)
    state.on_notify = lambda label: (_console.print(f"🧾 {label}") if _console else print(label))
    start_server(state, args.host, args.port)
    _banner(args.host, args.port, args.manager_port)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        if _console:
            _console.print("Вихід.")


if __name__ == "__main__":
    main()
