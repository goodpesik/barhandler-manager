"""Italian RT fiscal-printer capability (Epson RT / "Documento Commerciale").

PET-237 Phase C — device-manager side.

Unlike the Ukrainian path (`fiscal_receipt.py`), an Italian RT printer is a
*sealed fiscal device*: we don't render ESC/POS ourselves, we hand the printer
a signed **fiscal ePOS-Print XML** document over HTTP and it lays out + fiscally
signs the receipt (Documento Commerciale) itself. Epson exposes this through its
on-board **EpsonFPMate** web service (the "Fiscal ePOS-Print" API).

Flow, mirroring the UA renderer's shape (pure builder + thin transport + error
mapping):

  neutral payload ──build_*_xml()──▶ fiscal ePOS-Print XML
                  ──_soap_wrap()───▶ SOAP envelope
                  ──post_to_printer()─▶ HTTP POST to http://<ip>/cgi-bin/fpmate.cgi
                  ──parse_response()─▶ {receiptId, receiptNumber, raw}

The builders are pure (no I/O) so they can be golden-tested; the network call
lives in `post_to_printer()` and is the only thing the routes wrap in a thread.

IMPORTANT — the exact Epson ePOS-Print XML schema, the EpsonFPMate URL/query
string, the status-query command and the response field names are defined in
Epson's "Fiscal ePOS-Print XML" reference, which is not vendored here. Every
place where a literal (element/attribute name, URL, code) could not be verified
against that reference is marked with:
    # TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
The STRUCTURE below is correct and syntactically valid; treat the marked
literals as best-effort until confirmed on real hardware.
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
# TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
FPMATE_PATH = "/cgi-bin/fpmate.cgi"
FPMATE_QUERY = "devid=local_printer&timeout=10000"
# Fiscal ePOS-Print rides plain HTTP on port 80 (the printer's web service),
# NOT the raw ESC/POS port 9100 that the registry stores for network printers.
# TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
DEFAULT_HTTP_PORT = 80
DEFAULT_TIMEOUT_SECONDS = 15.0

# SOAP action for the ePOS-Print service.
# TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
SOAP_ACTION = '""'

# Sales are blocked once the last daily close is older than this (RT rule: a Z
# must be run at least every 24h; the printer starts refusing sales after the
# grace window). We surface it before the printer does.
BLOCKED_AFTER = timedelta(hours=48)

# Operator index printed on every fiscal command. RT printers key their journal
# per-operator; 1 is the conventional single-till default.
# TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
DEFAULT_OPERATOR = "1"

# Italian RT cash payment type (contante). Cash totals are rounded to 5 cents
# ("arrotondamento") before being sent as the tendered amount.
CASH_PAYMENT_TYPE = 0

# Fallback IVA-rate → department (reparto) map. The reparto is configured on the
# printer itself; this only kicks in when the caller doesn't pin a department on
# the line. Keys are the VAT percentage.
# TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
DEFAULT_IVA_TO_DEPARTMENT = {
    22.0: 1,
    10.0: 2,
    5.0: 3,
    4.0: 4,
    0.0: 5,
}

# Known EpsonFPMate error codes → operator-facing message. Code 17 is the one
# PET-237 cares about most: the printer has never had its first daily close, so
# it refuses every sale until a Z is run.
# TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
KNOWN_ERRORS = {
    "17": (
        "PRINTER ERROR 17 — first daily closure (Z report) has not been run. "
        "The RT printer refuses all sales until you execute the first Z "
        "(POST /fiscal/it/z)."
    ),
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
    # Best-effort textual fallback so a bare "cash"/"contanti" still lands on 0.
    if payment.type and payment.type.strip().lower() in {"cash", "contante", "contanti"}:
        return CASH_PAYMENT_TYPE
    return CASH_PAYMENT_TYPE


def _fmt_qty(value: float) -> str:
    # RT firmware expects fixed-point quantities.
    # TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
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
) -> str:
    """Build the fiscal ePOS-Print body for a Documento Commerciale.

    A normal sale uses `<printRecItem>` lines; a refund/void ("reso merce" —
    a negative document) uses `<printRecRefund>` lines instead. The total is
    emitted with the resolved RT payment type, and cash totals are rounded to
    5 cents first.

    Returns the inner `<printerFiscalReceipt>…` XML (SOAP wrapping happens in
    `_soap_wrap`).
    """
    # TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
    root = ET.Element("printerFiscalReceipt")

    ET.SubElement(root, "beginFiscalReceipt", {"operator": DEFAULT_OPERATOR})

    # `printRecItem` for a sale, `printRecRefund` for a negative document.
    # TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
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

    # TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
    ET.SubElement(
        root,
        "printRecTotal",
        {
            "operator": DEFAULT_OPERATOR,
            "description": payment.type or "Contante",
            "payment": _fmt_money(total),
            "paymentType": str(payment_type),
            "index": "0",
            "justification": "1",
        },
    )

    ET.SubElement(root, "endFiscalReceipt", {"operator": DEFAULT_OPERATOR})

    return ET.tostring(root, encoding="unicode")


def build_z_report_xml() -> str:
    """Daily close (Z report / chiusura giornaliera)."""
    # TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
    root = ET.Element("printerFiscalReport")
    ET.SubElement(root, "printZReport", {"operator": DEFAULT_OPERATOR})
    return ET.tostring(root, encoding="unicode")


def build_x_report_xml() -> str:
    """X read (non-resetting daily read / lettura giornaliera)."""
    # TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
    root = ET.Element("printerFiscalReport")
    ET.SubElement(root, "printXReport", {"operator": DEFAULT_OPERATOR})
    return ET.tostring(root, encoding="unicode")


def build_status_xml() -> str:
    """Query the printer status (used to derive last-Z / blocked)."""
    # TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
    root = ET.Element("printerCommand")
    ET.SubElement(root, "queryPrinterStatus", {"statusType": "1"})
    return ET.tostring(root, encoding="unicode")


def _soap_wrap(inner_xml: str) -> str:
    """Wrap a fiscal ePOS-Print body in the SOAP envelope EpsonFPMate expects."""
    # TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
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
    # TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
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
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": SOAP_ACTION,
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

    On `success="false"` (or an embedded PRINTER ERROR) raises `FiscalItError`
    with the mapped code/message. On success returns:
        {success, code, status, fields: {..addInfo..}, raw_xml}

    `fields` holds the flattened `<addInfo>` children (fiscalReceiptNumber,
    zRepNumber, lastZDate, …) the RT printer returns.
    TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
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

    success_attr = (resp.get("success") or "").strip().lower()
    success = success_attr in {"true", "1"}
    code = (resp.get("code") or "").strip()
    status = (resp.get("status") or "").strip()

    # Flatten addInfo (and any other child element carrying text) into `fields`.
    fields: dict = {}
    for child in resp.iter():
        name = _localname(child.tag)
        if name in {"response", "addInfo"}:
            continue
        text = (child.text or "").strip()
        if text:
            fields[name] = text

    raw = {"success": success, "code": code, "status": status, "fields": fields, "raw_xml": xml_text}

    if not success:
        # Prefer the explicit code, but also sniff the status text for a bare
        # "17" so a printer that reports the error in prose still gets mapped.
        mapped = KNOWN_ERRORS.get(code)
        if mapped is None and (code == "17" or "PRINTER ERROR 17" in status.upper()):
            mapped = KNOWN_ERRORS.get("17")
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
    TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
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
    )
    resp_text = post_to_printer(host, _soap_wrap(inner), port=port, timeout=timeout)
    parsed = parse_response(resp_text)
    receipt_id, receipt_number = _extract_receipt_ids(parsed["fields"])
    return {"receiptId": receipt_id, "receiptNumber": receipt_number, "raw": parsed}


def _extract_report_number(fields: dict) -> str:
    # TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
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
    TODO(confirm): Epson ePOS-Print XML — verify against Epson FP docs
    """
    combined = fields.get("lastZReportDate") or fields.get("lastZDate")
    date_part = fields.get("dailyClosureDate") or fields.get("lastClosureDate")
    time_part = fields.get("dailyClosureTime") or fields.get("lastClosureTime")

    candidates: list[str] = []
    if combined:
        candidates.append(combined)
    if date_part:
        candidates.append(f"{date_part} {time_part}".strip() if time_part else date_part)

    for raw in candidates:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d%m%Y %H%M"):
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
