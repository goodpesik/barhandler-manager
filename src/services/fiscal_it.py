"""Italian RT fiscal-printer capability (Epson RT / "Documento Commerciale").

PET-237 Phase C — device-manager side.

Unlike the Ukrainian path (`fiscal_receipt.py`), an Italian RT printer is a
*sealed fiscal device*: we don't render ESC/POS ourselves, we hand the printer
a **fiscal ePOS-Print XML** document over HTTP and it lays out + fiscally signs
the receipt (Documento Commerciale) itself. Epson exposes this through its
on-board **EpsonFPMate** web service (the "Fiscal ePOS-Print" API).

Flow, mirroring the UA renderer's shape (pure builder + thin transport + error
mapping):

  neutral payload ──build_*_xml()──▶ fiscal ePOS-Print XML
                  ──_soap_wrap()───▶ SOAP envelope
                  ──post_to_printer()─▶ HTTP POST to http://<ip>/cgi-bin/fpmate.cgi
                  ──parse_response()─▶ {receiptId, receiptNumber, raw}

The builders are pure (no I/O) so they can be golden-tested against the local
`emulator.fiscal_epos` emulator; the network call lives in `post_to_printer()`
and is the only thing the routes wrap in a thread.

SPEC PROVENANCE — the element/attribute names, the SOAP transport, the response
schema and error semantics below were verified against multiple real sources:
Epson's own SEIKO `fiscalprint.js`, the Odoo `fp90iii.js` driver, a C#
EpsonFiscalPrinter lib, Microsoft D365's Epson FP-90III sample, a
real-hardware-verified Italian FPMate bridge (`tecnosiel/OfficinaPro`
`FPmateXmlBuilder.java`, "Verificato sul registratore reale FP-81II"), and the
efsta EPSON error-code table. Epson's own PDF guides sit behind an Akamai block
(403 to curl AND real headless Chrome), so those primary PDFs were unreachable —
but every literal below is cross-checked against the working sources above, so
there are no remaining guesses.
"""

from __future__ import annotations

import logging
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transport constants
# ---------------------------------------------------------------------------

# EpsonFPMate / Fiscal ePOS-Print endpoint. `devid=local_printer` addresses the
# printer's own fiscal device; `timeout` is the printer-side wait in ms.
FPMATE_PATH = "/cgi-bin/fpmate.cgi"
FPMATE_QUERY = "devid=local_printer&timeout=10000"
# Fiscal ePOS-Print rides plain HTTP on port 80 (the printer's web service),
# NOT the raw ESC/POS port 9100 that the registry stores for network printers.
DEFAULT_HTTP_PORT = 80
DEFAULT_TIMEOUT_SECONDS = 15.0

# SOAP action for the ePOS-Print service — literally an empty quoted string.
SOAP_ACTION = '""'
# Epson's own JS client sends this fixed value so caches never serve a stale
# fiscal response.
IF_MODIFIED_SINCE = "Thu, 01 Jan 1970 00:00:00 GMT"

# Sales are blocked once the last daily close is older than this (RT rule: a Z
# must be run at least every 24h; the printer starts refusing sales after the
# grace window). We surface it before the printer does.
BLOCKED_AFTER = timedelta(hours=48)

# Operator index printed on every fiscal command. RT printers key their journal
# per-operator; 1 is the conventional single-till default.
DEFAULT_OPERATOR = "1"

# Italian RT payment types (Epson `paymentType`): 0 = contanti (cash),
# 1 = assegno (cheque), 2 = carta / pagamento elettronico (card). Cash totals
# are rounded to 5 cents ("arrotondamento") before being tendered.
CASH_PAYMENT_TYPE = 0
CARD_PAYMENT_TYPE = 2

# Fallback IVA-rate → department (reparto) map. The VAT rate is configured on
# the printer *per department*; the line only carries `department="N"`. These
# are the Italian conventional repartos (per Microsoft's Epson FP-90III sample:
# 01=22%, 03=10%, 05=5%, 07=4%, 09=0/esente). Used only when the caller leaves a
# line's department blank — normally the shop's Tax.rtDepartment is passed in and
# must match the printer's own reparto configuration.
DEFAULT_IVA_TO_DEPARTMENT = {
    22.0: 1,
    10.0: 3,
    5.0: 5,
    4.0: 7,
    0.0: 9,
}

# EpsonFPMate fiscal error codes → operator-facing message. Sourced from the
# efsta EPSON error-code table (docs.efsta.eu/efr/IT/fiscal_epson) — the Italian
# RT registrar-di-cassa (RT) error set. Code 17 ("IMPOSSIBILE ORA") is a GENERIC
# "operation not possible at this time" state error: a missing first daily-close
# is ONE common cause, but so is a mis-flagged refund — so we surface the printer
# status/last-command rather than hard-asserting the cause.
KNOWN_ERRORS = {
    "02": "RT error 02 (CARTA SCONTRINO) — receipt paper is low.",
    "03": "RT error 03 (OFFLINE) — printer offline: paper empty or cover open.",
    "09": "RT error 09 (DATA INFERIORE) — date earlier than the last fiscal closure.",
    "10": "RT error 10 (DATA ERRATA) — bad date format.",
    "11": "RT error 11 (SEQUENZA ERRATA) — command not allowed at this point in the sequence.",
    "12": "RT error 12 (DATI INESISTENTI) — inexistent data (e.g. unprogrammed PLU).",
    "13": "RT error 13 (VALORE ERRATO) — one or more fields hold an invalid value.",
    "14": "RT error 14 (PROG MATRICOLA) — no fiscal serial number programmed.",
    "16": "RT error 16 (NON PREVISTO) — invalid index or unknown command pair.",
    "17": (
        "RT error 17 (IMPOSSIBILE ORA) — the operation is not possible in the "
        "printer's current state. Common causes: the first daily closure (Z "
        "report) has not been run yet, or a refund/void was sent with the wrong "
        "flag. Check the printer status and run a Z (POST /fiscal/it/z) if this "
        "is the first use of the day."
    ),
    "18": "RT error 18 (NON POSSIBILE) — the operation cannot be carried out.",
    "20": "RT error 20 (SUPERA VALORE) — amount above the maximum allowed.",
    "21": "RT error 21 (SUPERA LIMITE) — a parameter is outside the permitted range.",
    "22": "RT error 22 (NON PROGRAMMATO) — the command requires prior programming.",
    "23": "RT error 23 (CHIUDI SCONTRINO) — max operations reached; close or cancel the receipt.",
    "24": "RT error 24 (CHIUDI PAGAMENTO) — max operations reached during partial payments.",
    "26": "RT error 26 (CASSA INFERIORE) — cash-out exceeds the current drawer total.",
    "27": "RT error 27 (OLTRE PROGRAMMAZIONE) — line total above the department limit.",
    "28": "RT error 28 (P.C. NON CONNESSO) — no PC/server connection or bad sequence end.",
    "30": "RT error 30 (CHECKSUM ERRATO) — Partita IVA / codice fiscale checksum error.",
    "34": "RT error 34 (MANCA ATTIVAZIONE) — missing activation for this operation.",
    "38": "RT error 38 (EFT-POS in ERRORE) — the card terminal reported an error.",
}


# ---------------------------------------------------------------------------
# Neutral payload
# ---------------------------------------------------------------------------


@dataclass
class ItItem:
    """One sale line in printer-neutral terms."""

    name: str
    quantity: float
    unit_price: float
    total_price: float
    iva_rate: float
    department: Optional[int] = None


@dataclass
class ItPayment:
    """How the customer paid. `type` is the caller's own tender key
    (e.g. "cash" / "card" / "contanti"); `payment_type_map` resolves it to
    the RT payment-type integer."""

    type: str
    amount: float


@dataclass
class ItDocument:
    items: list[ItItem]
    payment: ItPayment
    payment_type_map: Optional[dict] = field(default=None)
    is_refund: bool = False
    # Optional "RESO MERCE N.<z>-<doc> del <date>" reference tying a refund to
    # the original document; printed as the reso header when present.
    refund_reference: Optional[str] = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FiscalItError(Exception):
    """A fiscal operation the printer rejected (or a transport failure).

    Carries a structured `code` so the route layer can echo it in the 5xx
    `detail` envelope the frontend already switches on (mirrors
    `PrinterUnavailable` in the ESC/POS path)."""

    def __init__(self, message: str, *, code: str = "fiscal_error", raw: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.raw = raw or {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def round_to_5_cents(value: float) -> float:
    """Italian cash rounding — round to the nearest 5 cents (0.05 €)."""
    return round(round(value / 0.05) * 0.05, 2)


def department_for_iva(iva_rate: float) -> int:
    """Fallback reparto for a line whose department the caller left blank."""
    return DEFAULT_IVA_TO_DEPARTMENT.get(float(iva_rate), 1)


def resolve_payment_type(payment: ItPayment, payment_type_map: Optional[dict]) -> int:
    """Map the caller's tender key to an RT payment-type integer.

    Unknown / missing keys fall back to cash (0) — the RT printer always
    accepts a cash total, so an unmapped tender degrades to cash rather than
    failing the whole receipt."""
    if payment_type_map:
        mapped = payment_type_map.get(payment.type)
        if mapped is not None:
            return int(mapped)
    key = (payment.type or "").strip().lower()
    if key in {"cash", "contante", "contanti"}:
        return CASH_PAYMENT_TYPE
    if key in {"card", "carta", "electronic", "pos"}:
        return CARD_PAYMENT_TYPE
    return CASH_PAYMENT_TYPE


def _fmt_qty(value: float) -> str:
    # RT firmware expects fixed-point quantities (Epson uses '.' decimals in the
    # request; the printer echoes ',' decimals in the response).
    return f"{value:.3f}"


def _fmt_money(value: float) -> str:
    return f"{value:.2f}"


# ---------------------------------------------------------------------------
# XML builders (pure — no I/O)
# ---------------------------------------------------------------------------


def build_commercial_document_xml(
    items: list[ItItem],
    payment: ItPayment,
    *,
    payment_type_map: Optional[dict] = None,
    is_refund: bool = False,
    refund_reference: Optional[str] = None,
) -> str:
    """Build the fiscal ePOS-Print body for a Documento Commerciale.

    A normal sale uses `<printRecItem>` lines; a refund ("reso merce") is a
    `<printerFiscalReceipt>` prefixed with a `<printRecMessage messageType="4">`
    reso header and using `<printRecRefund>` lines. The total is emitted with
    the resolved RT payment type, and cash totals are rounded to 5 cents.

    Returns the inner `<printerFiscalReceipt>…` XML (SOAP wrapping happens in
    `_soap_wrap`).
    """
    root = ET.Element("printerFiscalReceipt")

    # A refund is a normal fiscal receipt preceded by the "RESO MERCE" header
    # (messageType 4) that ties it to the original document.
    if is_refund:
        ET.SubElement(
            root,
            "printRecMessage",
            {
                "operator": DEFAULT_OPERATOR,
                "messageType": "4",
                "message": refund_reference or "RESO MERCE",
            },
        )

    ET.SubElement(root, "beginFiscalReceipt", {"operator": DEFAULT_OPERATOR})

    # `printRecItem` for a sale, `printRecRefund` for a reso (negative document).
    line_tag = "printRecRefund" if is_refund else "printRecItem"
    for item in items:
        department = item.department
        if department is None:
            department = department_for_iva(item.iva_rate)
        ET.SubElement(
            root,
            line_tag,
            {
                "operator": DEFAULT_OPERATOR,
                "description": item.name,
                "quantity": _fmt_qty(item.quantity),
                "unitPrice": _fmt_money(item.unit_price),
                "department": str(int(department)),
                "justification": "1",
            },
        )

    payment_type = resolve_payment_type(payment, payment_type_map)
    total = payment.amount
    if payment_type == CASH_PAYMENT_TYPE:
        total = round_to_5_cents(total)

    # printRecTotal carries the tender + amount; reaching the total settles and
    # closes the fiscal receipt.
    ET.SubElement(
        root,
        "printRecTotal",
        {
            "operator": DEFAULT_OPERATOR,
            "description": "RIMBORSO" if is_refund else (payment.type or "Contante"),
            "payment": _fmt_money(total),
            "paymentType": str(payment_type),
            "index": "1",
            "justification": "1",
        },
    )

    ET.SubElement(root, "endFiscalReceipt", {"operator": DEFAULT_OPERATOR})

    return ET.tostring(root, encoding="unicode")


def build_z_report_xml() -> str:
    """Daily close (Z report / chiusura giornaliera)."""
    root = ET.Element("printerFiscalReport")
    ET.SubElement(root, "printZReport", {"operator": DEFAULT_OPERATOR})
    return ET.tostring(root, encoding="unicode")


def build_x_report_xml() -> str:
    """X read (non-resetting daily read / lettura giornaliera)."""
    root = ET.Element("printerFiscalReport")
    ET.SubElement(root, "printXReport", {"operator": DEFAULT_OPERATOR})
    return ET.tostring(root, encoding="unicode")


def build_status_xml() -> str:
    """Query the printer status (used to derive last-Z / blocked).

    The status verb is a bare `<queryPrinterStatus />` and — unlike the print
    commands — it MUST be wrapped in `<printerCommand>`; without that wrapper the
    RT registrar returns `code="INCOMPLETE FILE"` (verified on a real Epson
    FP-81II RT via the tecnosiel/OfficinaPro FPMate bridge). `_soap_wrap` then
    puts this inside the SOAP body.
    """
    root = ET.Element("printerCommand")
    ET.SubElement(root, "queryPrinterStatus")
    return ET.tostring(root, encoding="unicode")


def _soap_wrap(inner_xml: str) -> str:
    """Wrap a fiscal ePOS-Print body in the SOAP envelope EpsonFPMate expects."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        f"{inner_xml}"
        "</s:Body>"
        "</s:Envelope>"
    )


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _endpoint_url(host: str, port: int = DEFAULT_HTTP_PORT) -> str:
    base = f"http://{host}" if port == 80 else f"http://{host}:{port}"
    return f"{base}{FPMATE_PATH}?{FPMATE_QUERY}"


def post_to_printer(
    host: str,
    soap_body: str,
    *,
    port: int = DEFAULT_HTTP_PORT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """POST a SOAP-wrapped fiscal document to the printer, return the raw XML
    response text. Synchronous (stdlib urllib) — routes call it in a thread.

    Raises `FiscalItError(code="printer_unreachable")` on any transport error so
    the route layer maps it to a 503 the same way the ESC/POS path does."""
    url = _endpoint_url(host, port)
    data = soap_body.encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": SOAP_ACTION,
            "If-Modified-Since": IF_MODIFIED_SINCE,
            "Content-Length": str(len(data)),
        },
    )
    logger.info("fiscal_it: POST %s (%d bytes)", url, len(data))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # urllib raises a zoo of errors; treat all as transport
        logger.warning("fiscal_it: transport error talking to %s: %r", url, exc)
        raise FiscalItError(
            f"RT printer unreachable at {host}: {exc}",
            code="printer_unreachable",
        )
    logger.debug("fiscal_it: response %d bytes", len(body))
    return body


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _localname(tag: str) -> str:
    """Strip any XML namespace so we can match on bare element names."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_response_element(root: ET.Element) -> Optional[ET.Element]:
    if _localname(root.tag) == "response":
        return root
    for el in root.iter():
        if _localname(el.tag) == "response":
            return el
    return None


def parse_response(xml_text: str) -> dict:
    """Parse an EpsonFPMate response into a structured dict.

    The printer replies with `<response success="true|false" code="" status="">`
    wrapping an `<addInfo>` block whose children carry the fiscal fields
    (fiscalReceiptNumber, zRepNumber, fiscalReceiptDate/Time, …). On
    `success="false"` we raise `FiscalItError` with the mapped code/message; on
    success we return {success, code, status, fields, raw_xml}.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FiscalItError(
            f"could not parse RT printer response: {exc}",
            code="bad_response",
            raw={"raw_xml": xml_text},
        )

    resp = _find_response_element(root)
    if resp is None:
        raise FiscalItError(
            "RT printer response missing <response> element",
            code="bad_response",
            raw={"raw_xml": xml_text},
        )

    # Epson's own parser accepts "true" or "1" for success.
    success_attr = (resp.get("success") or "").strip().lower()
    success = success_attr in {"true", "1"}
    code = (resp.get("code") or "").strip()
    status = (resp.get("status") or "").strip()

    # Flatten addInfo (and any other child element carrying text) into `fields`.
    # `elementList` is a CSV of which children are present — skip it as a value
    # but let the real fields through.
    fields: dict = {}
    for child in resp.iter():
        name = _localname(child.tag)
        if name in {"response", "addInfo", "elementList"}:
            continue
        text = (child.text or "").strip()
        if text:
            fields[name] = text

    raw = {"success": success, "code": code, "status": status, "fields": fields, "raw_xml": xml_text}

    if not success:
        mapped = KNOWN_ERRORS.get(code)
        message = mapped or (
            f"RT printer rejected the operation (code={code or '?'}, status={status or '?'})"
        )
        raise FiscalItError(message, code=f"rt_{code}" if code else "rt_error", raw=raw)

    return raw


# ---------------------------------------------------------------------------
# High-level operations (build + post + parse)
# ---------------------------------------------------------------------------


def _extract_receipt_ids(fields: dict) -> tuple[str, str]:
    """Pull (receiptId, receiptNumber) out of the parsed addInfo fields.

    receiptNumber is the printer's fiscal receipt counter; receiptId is a
    best-effort unique key combining the Z-report number and the receipt
    number (RT receipts are uniquely identified by the pair).
    """
    receipt_number = (
        fields.get("fiscalReceiptNumber")
        or fields.get("receiptNumber")
        or fields.get("docNumber")
        or ""
    )
    z_number = fields.get("zRepNumber") or fields.get("zRepNr") or ""
    receipt_id = "-".join(p for p in (z_number, receipt_number) if p) or receipt_number
    return receipt_id, receipt_number


def print_commercial_document(
    host: str,
    document: ItDocument,
    *,
    port: int = DEFAULT_HTTP_PORT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Print a Documento Commerciale and return {receiptId, receiptNumber, raw}."""
    inner = build_commercial_document_xml(
        document.items,
        document.payment,
        payment_type_map=document.payment_type_map,
        is_refund=document.is_refund,
        refund_reference=document.refund_reference,
    )
    resp_text = post_to_printer(host, _soap_wrap(inner), port=port, timeout=timeout)
    parsed = parse_response(resp_text)
    receipt_id, receipt_number = _extract_receipt_ids(parsed["fields"])
    return {"receiptId": receipt_id, "receiptNumber": receipt_number, "raw": parsed}


def _extract_report_number(fields: dict) -> str:
    return (
        fields.get("zRepNumber")
        or fields.get("reportNumber")
        or fields.get("fiscalReportNumber")
        or ""
    )


def run_z_report(host: str, *, port: int = DEFAULT_HTTP_PORT, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Daily close. Returns {reportNumber, raw}."""
    resp_text = post_to_printer(host, _soap_wrap(build_z_report_xml()), port=port, timeout=timeout)
    parsed = parse_response(resp_text)
    return {"reportNumber": _extract_report_number(parsed["fields"]), "raw": parsed}


def run_x_report(host: str, *, port: int = DEFAULT_HTTP_PORT, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """X read. Returns {reportNumber, raw}."""
    resp_text = post_to_printer(host, _soap_wrap(build_x_report_xml()), port=port, timeout=timeout)
    parsed = parse_response(resp_text)
    return {"reportNumber": _extract_report_number(parsed["fields"]), "raw": parsed}


def _parse_last_z(fields: dict) -> Optional[datetime]:
    """Best-effort parse of the last-Z timestamp out of status fields.

    RT printers report this as separate date+time fields (DD/MM/YYYY, HH:MM)
    or a combined ISO string depending on model/firmware.
    """
    combined = (
        fields.get("receiptISODateTime")
        or fields.get("lastZReportDate")
        or fields.get("lastZDate")
    )
    date_part = (
        fields.get("fiscalReceiptDate")
        or fields.get("dailyClosureDate")
        or fields.get("lastClosureDate")
    )
    time_part = (
        fields.get("fiscalReceiptTime")
        or fields.get("dailyClosureTime")
        or fields.get("lastClosureTime")
    )

    candidates: list[str] = []
    if combined:
        candidates.append(combined)
    if date_part:
        candidates.append(f"{date_part} {time_part}".strip() if time_part else date_part)

    for raw in candidates:
        for fmt in (
            "%Y%m%dT%H%M%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y",
        ):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def query_status(host: str, *, port: int = DEFAULT_HTTP_PORT, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Query RT status. Returns {lastZAt, blocked, raw}.

    `blocked` is True when the last daily close is older than BLOCKED_AFTER
    (48h) — at that point the printer refuses sales, so we warn the operator
    before they even try to sell."""
    resp_text = post_to_printer(host, _soap_wrap(build_status_xml()), port=port, timeout=timeout)
    parsed = parse_response(resp_text)
    last_z = _parse_last_z(parsed["fields"])
    if last_z is None:
        # Unknown last-Z → we can't prove it's safe; report not-blocked but let
        # the raw payload carry the truth for the caller to inspect.
        blocked = False
        last_z_iso = None
    else:
        blocked = (datetime.now(timezone.utc) - last_z) > BLOCKED_AFTER
        last_z_iso = last_z.isoformat()
    return {"lastZAt": last_z_iso, "blocked": blocked, "raw": parsed}
