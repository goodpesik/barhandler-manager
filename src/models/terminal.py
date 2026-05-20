"""POS terminal models — discovery, registration, charge request/result.

The data shapes mirror the SSI ECR JSON protocol vocabulary (because
that's the only adapter we ship today), but the public types are bank-
agnostic — `TerminalKind` carries the brand so the future PrivatBank /
Raiffeisen adapters slot into the same registry without breaking
clients. SSI-specific payload fields like `bankProfileId` /
`splitData` are kept under a free-form `vendor_data` dict in the
result so the route layer doesn't have to chase every new doc revision.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class TerminalKind(str, Enum):
    """Bank brand using the SSI protocol on a given physical device.

    `mono_pos` and `privat_pos` are the same wire format — the label
    only affects which `merchantId` defaults and (eventually) which
    error-code translations show up in the UI. Adding a new bank
    later is one enum entry + (optional) tiny adapter subclass.
    """

    mono_pos = "mono_pos"
    privat_pos = "privat_pos"
    raif_pos = "raif_pos"
    pivdenny_pos = "pivdenny_pos"
    generic_ssi = "generic_ssi"  # any other SSI-protocol terminal


class TerminalTransport(str, Enum):
    network = "network"  # TCP 3000 framed or HTTP 3001 plain
    usb = "usb"  # USB-C with vendor driver — out of scope for now


class TerminalNetworkAddress(BaseModel):
    host: str
    port: int = 3000  # TCP framed default; HTTP variant uses 3001
    use_http: bool = False


class TerminalDescriptor(BaseModel):
    """What `/terminal/discover` returns. `id` is the stable identifier
    the frontend persists; same physical terminal → same id across
    reboots even if its DHCP lease shifts (we'd then re-discover and
    update the network address)."""

    model_config = ConfigDict(use_enum_values=True)

    id: str
    transport: TerminalTransport
    label: str  # human-readable, e.g. "Mono POS @ 192.168.0.42"
    kind: Optional[TerminalKind] = None  # may be unknown until first probe
    model: Optional[str] = None  # from GetTerminalInfo: "Verifone X990" etc.
    serial: Optional[str] = None  # terminalSerialNumber / pos_sn
    network: Optional[TerminalNetworkAddress] = None


class MerchantBinding(BaseModel):
    """Operator-named binding to one SSI merchantId/terminalId pair.

    A single physical POS terminal can host several merchants (a
    restaurant ФОП + a takeaway-window ФОП on the same device — both
    legal in Ukraine). The terminal returns them via
    `GetMerchantListDetailed` with bank-side names that are usually
    legal-entity boilerplate; the operator overrides those with a
    short nickname ("Бар", "Тераса") that's actually pickable at the
    cash drawer.
    """

    merchant_id: str
    terminal_id: Optional[str] = None  # SSI multi-merchant-multi-terminal case
    nickname: Optional[str] = None     # operator override; falls back to
    merchant_name: Optional[str] = None  # the bank-side name (from SSI)


class TerminalRegistration(BaseModel):
    """Operator-saved binding: descriptor + per-merchant defaults."""

    descriptor: TerminalDescriptor
    kind: TerminalKind = TerminalKind.generic_ssi
    nickname: Optional[str] = None  # "Каса 1" / "Бар"
    default_merchant_id: Optional[str] = None  # picked from GetMerchantListDetailed
    default_terminal_id: Optional[str] = None  # multi-merchant-multi-terminal case
    merchants: list[MerchantBinding] = Field(default_factory=list)
    # ^ Snapshot of what's on the terminal at registration time, with
    # operator-set nicknames preserved across reboots. The route layer
    # refreshes this on every /merchants call so the bank-side names
    # stay current, but nicknames are sticky.


class TerminalRegistrationRequest(BaseModel):
    """Body for POST /terminal/register."""

    id: str = Field(min_length=1)
    kind: TerminalKind = TerminalKind.generic_ssi
    nickname: Optional[str] = None
    default_merchant_id: Optional[str] = None
    default_terminal_id: Optional[str] = None
    merchants: Optional[list[MerchantBinding]] = None


class MerchantNicknameUpdate(BaseModel):
    """Body for PUT /terminal/{id}/merchants. Operator submits the full
    list — server merges nicknames in by merchant_id+terminal_id."""

    merchants: list[MerchantBinding]


class ChargeRequest(BaseModel):
    """What the frontend sends to `/terminal/{id}/charge`. Bank-agnostic
    shape — `splitData` and other SSI-only knobs go in `extras` so we
    don't have to teach the model every doc revision."""

    amount_kopecks: int = Field(gt=0)
    currency: str = "980"  # ISO 4217, UAH default
    merchant_id: Optional[str] = None  # falls back to registration.default_merchant_id
    transaction_uid: Optional[str] = None  # our external ID; terminal echoes back
    discounted_amount_kopecks: Optional[int] = None  # external-system discount calc
    extras: dict = Field(default_factory=dict)  # splitData etc — passed through


class AcquirerResult(BaseModel):
    """Unified outcome the FiscalReceipt `acquirer` block consumes."""

    status: str  # "ok" | "declined" | "cancelled"
    transaction_uid: Optional[str] = None
    rrn: Optional[str] = None
    auth_code: Optional[str] = None
    cardmask: Optional[str] = None
    paysys: Optional[str] = None  # "Visa" / "MasterCard"
    bank_name: Optional[str] = None
    terminal_id: Optional[str] = None
    pos_entry_mode: Optional[str] = None  # "CONTACTLESS" / "CONTACT_EMV" / ...
    invoice_num: Optional[str] = None
    response_code: Optional[str] = None
    raw_transaction_result: Optional[str] = None  # SSI enum value verbatim
    error_code: Optional[str] = None  # E00-E22 if non-ok
    error_message: Optional[str] = None
    error_details: Optional[str] = None
    # PrivatBank embedded-fiscal fields. Populated from Purchase response's
    # `adv` field (host advert 63.29) when the merchant has activated "Каса"
    # bank-side; absent for SSI / for PrivatBank merchants without "Каса".
    # `fiscal_receipt_id` → Національний е-чек ID (ДПС). Surface this in
    # Reports so the operator can cross-reference in Приват24.
    fiscal_receipt_id: Optional[str] = None  # adv.natr — ДПС fiscal
    bank_receipt_id: Optional[int] = None    # adv.rid — bank е-чек
    fiscal_receipt_text: Optional[str] = None  # GetReceiptInfo.receipt — printable text
    vendor_data: dict = Field(default_factory=dict)  # full GetLastResult.params


def make_terminal_id(transport: TerminalTransport, *parts: str) -> str:
    """Stable 12-char hex id from transport + immutable address parts.

    For SSI terminals over LAN we hash transport + host + port + serial
    (when known after the first GetTerminalInfo). Serial gives stability
    across DHCP changes; before we know it, host+port is the fallback —
    rediscovery will re-hash and update.
    """
    payload = ":".join([transport.value, *parts]).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:12]
