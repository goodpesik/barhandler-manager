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
 body{background:#111;color:#ddd;font-family:ui-monospace,Menlo,monospace;margin:0;padding:16px}
 h1{font-size:15px;color:#8fd08f;margin:0 0 12px}
 .doc{background:#1c1c1c;border:1px solid #333;border-radius:8px;padding:12px;margin:0 0 12px;max-width:420px}
 .doc.reso{border-color:#a6772e}.doc.rej{border-color:#b23}.doc.z{border-color:#3a7}
 .k{color:#8fd08f;font-weight:700}.muted{color:#888}
 table{width:100%;border-collapse:collapse;margin:6px 0}
 td{padding:1px 0;font-size:13px}.r{text-align:right}
 .tot{border-top:1px dashed #444;margin-top:6px;padding-top:6px;font-weight:700}
</style></head><body>
<h1>🧾 Epson Fiscal ePOS-Print emulator — Documento Commerciale journal</h1>
<div id="roll"></div>
<script>
async function poll(){
 try{const r=await fetch('state');const docs=await r.json();
  document.getElementById('roll').innerHTML=docs.map(function(d){
   var cls=d.kind==='RESO'?'reso':(d.kind==='REJECTED'?'rej':(d.kind.indexOf('CHIUSURA')>=0?'z':''));
   var rows=(d.items||[]).map(function(it){return '<tr><td>'+it.description+' ×'+it.quantity+
     ' <span class="muted">rep '+it.department+'</span></td><td class="r">'+it.unitPrice+'</td></tr>';}).join('');
   var head='<div><span class="k">'+d.kind+'</span> '+(d.number?('#'+d.number):'')+
     ' <span class="muted">'+(d.ts||'')+'</span></div>';
   var tot=d.total?('<div class="tot">TOTALE <span class="r" style="float:right">'+d.total+
     (d.payment_type!=null?(' <span class="muted">pt'+d.payment_type+'</span>'):'')+'</span></div>'):'';
   var rej=d.reason?('<div class="muted">'+d.reason+'</div>'):'';
   return '<div class="doc '+cls+'">'+head+(rows?('<table>'+rows+'</table>'):'')+tot+rej+'</div>';
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
    # Register this emulator in the manager as a fiscal_it printer by IP:port —
    # discovery can't find an HTTP fpmate device, so it must be added manually.
    register = (
        f"curl -X POST http://127.0.0.1:{manager_port}/devices/register-manual "
        f"-H 'X-Api-Key: {API_KEY}' -H 'Content-Type: application/json' "
        f"-d '{{\"host\":\"{host}\",\"port\":{port},\"kind\":\"fiscal_it\","
        f"\"nickname\":\"RT emulator\"}}'"
    )
    if _console is None:
        print(f"Fiscal ePOS emulator on {url}\nRegister in manager:\n{register}")
        return
    body = Text()
    body.append("🏦 BARHANDLER — FISCAL ePOS-PRINT EMULATOR\n", style="bold")
    body.append("device side of an Italian Epson RT printer\n\n", style="dim")
    body.append("FPMate endpoint: ", style="")
    body.append(f"{url}\n\n", style="bold green")
    body.append("Register in the manager (discovery can't find it):\n", style="dim")
    body.append(f"{register}\n", style="yellow")
    body.append(
        "\nthen bind this printer to the Italy fiscal card in Settings.\n",
        style="dim",
    )
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
