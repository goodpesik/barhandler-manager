"""POS terminal HTTP API.

Mirrors the printer route surface — discover / register / list /
unregister + the per-terminal action endpoints (charge, cancel,
status, list-merchants, ping). The route layer is bank-agnostic;
adapter selection happens inside the registry.

The terminal registry instance is attached to `app.state.terminal_registry`
during app startup (lifespan in `src/server.py`). When that hookup
isn't present yet we fall back to a lazily-built default — keeps the
old `/charge` stub functional during migration.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from pydantic import BaseModel, Field

from src.devices.terminal_registry import TerminalRegistry, UnknownTerminal
from src.models.terminal import (
    ChargeRequest,
    RefundRequest,
    MerchantBinding,
    MerchantNicknameUpdate,
    TerminalKind,
    TerminalRegistrationRequest,
)
from src.services import work_journal
from src.services.charge_claim import (
    STATE_DONE,
    STATE_UNRESOLVED,
    ChargeClaims,
    kept_result,
    says_what_happened,
    usable_key,
)
from src.services.terminals.base import NOTHING_WAS_SENT_CODES, TerminalUnavailable

# Same channel the charge route already logs on, so the line shape the
# shops' logs are read by does not change.
_log = logging.getLogger(__name__)


class ManualTerminalRequest(BaseModel):
    """Body for POST /terminal/register-manual.

    Use when the scan can't reach the terminal (CGNAT / mobile hotspot /
    VLAN). Operator looks up the terminal's IP in its admin menu and
    types it in. `kind` picks the SSI variant — picks `mono_pos` /
    `privat_pos` /  `raif_pos` / `pivdenny_pos` / `generic_ssi`.
    """
    host: str = Field(min_length=1)
    port: int = Field(default=3000, ge=1, le=65535)
    kind: TerminalKind = TerminalKind.generic_ssi
    nickname: Optional[str] = None
    label: Optional[str] = None


class SerialTerminalRequest(BaseModel):
    """Body for POST /terminal/register-serial — a USB terminal reached over a
    virtual COM port (Windows). Same ECR JSON as TCP, driven over the port."""

    port: str = Field(min_length=1)      # "COM4" / "/dev/ttyACM0"
    baudrate: int = Field(default=115200)
    kind: TerminalKind = TerminalKind.privat_pos
    nickname: Optional[str] = None
    label: Optional[str] = None

router = APIRouter()


def _registry(request: Request) -> TerminalRegistry:
    """Pull the singleton registry off app state, building one on
    demand so we never blow up on a fresh install."""
    registry = getattr(request.app.state, "terminal_registry", None)
    if registry is None:
        registry = TerminalRegistry()
        registry.load()
        request.app.state.terminal_registry = registry
    return registry


def _resolve(request: Request, terminal_id: Optional[str]):
    """Pick the requested terminal (or the only registered one if the
    caller didn't specify) and instantiate its adapter."""
    registry = _registry(request)
    if terminal_id is None:
        reg = registry.first()
        if reg is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "no_terminal",
                    "message": "no terminal registered — POST /terminal/register first",
                },
            )
        terminal_id = reg.descriptor.id
    try:
        return registry.adapter_for(terminal_id), terminal_id
    except UnknownTerminal as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_terminal", "message": str(exc)},
        )


def _surface_terminal_error(
    exc: TerminalUnavailable,
    *,
    operation: str = "terminal",
    terminal_id: Optional[str] = None,
) -> HTTPException:
    """Map a TerminalUnavailable onto the same `{detail: {code, message}}`
    shape the printer routes use, so the frontend's existing 503
    error-handler picks both up uniformly.

    The failure is logged here, which is the only place every endpoint
    passes through. Until this line existed only `/charge` logged, so a
    till whose terminal was unreachable answered the cashier 503 on
    `/merchants`, `/ping` and `/refund` and left nothing in the manager's
    own log — the failures had to be reconstructed from the uplink
    service on the server side.
    """
    _log.warning(
        "[%s] terminal=%s TerminalUnavailable code=%s msg=%s",
        operation, terminal_id, getattr(exc, "code", None), exc,
    )
    return HTTPException(
        status_code=503,
        detail={"code": getattr(exc, "code", "unavailable"), "message": str(exc)},
    )


# One card operation per terminal at a time.
#
# A second click on Pay used to be stopped by the terminal itself: it refuses a
# second ECR connection, and that refusal came back in about 3 s. With the
# bounded connect retry a refused connect is tried for up to 14 s, and a charge
# on these terminals takes 9-15 s, so the second Purchase can now reach the
# terminal just as the first one finishes — a double charge on one basket.
# Nothing in front of this stops it either: the till disables its own Pay
# button, but a second browser tab or a second till does not go through that
# button.
#
# The second caller is refused immediately and is never queued: a queued second
# charge is the same double charge a few seconds later. The claim is keyed by
# the terminal, because the terminal is what cannot do two card operations at
# once — so a refund lands on the same slot as a charge. Check and claim run
# with no `await` between them, so two requests on the event loop cannot both
# get the slot.
#
# THIS SET IS IN MEMORY, AND THAT IS DELIBERATE. It is the overlap of two
# operations it stops, which only exists while a request of this process is
# running; a process that dies takes the set with it and holds no terminal
# afterwards. What it cannot see — the same operation carried out twice, the
# second time after the first has gone — is what the operation's key stops, in
# `src/services/charge_claim.py` (PET-1304).
_busy_terminals: set[str] = set()


def _claim_terminal(terminal_id: str, operation: str) -> None:
    """Take the card-operation slot for this terminal, or refuse the caller."""
    if terminal_id in _busy_terminals:
        _log.warning(
            "[%s] terminal=%s refused: the terminal is already running a card "
            "operation, and a second one could take the money twice",
            operation, terminal_id,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "terminal_busy",
                "message": (
                    "the terminal is already running a card operation; "
                    "wait for it to finish"
                ),
            },
        )
    _busy_terminals.add(terminal_id)


def _release_terminal(terminal_id: str) -> None:
    """Hand the slot back. Runs in a `finally`, so it also runs when the
    cashier's browser closes mid-payment."""
    _busy_terminals.discard(terminal_id)


def _claims(request: Request) -> ChargeClaims:
    """The station's claimed operation keys, built on demand so a manager
    started without our lifespan (tests, an old embedded build) still has
    one — the same shape as `_registry` above."""
    claims = getattr(request.app.state, "charge_claims", None)
    if claims is None:
        claims = ChargeClaims()
        claims.load()
        request.app.state.charge_claims = claims
    return claims


def _claim_operation(
    request: Request,
    *,
    operation: str,
    terminal_id: str,
    key: Optional[str],
    amount_kopecks: int,
) -> Optional[str]:
    """Take the terminal's card slot and the operation's key, or refuse.

    PET-1304. Two guards, and they stop different things. The slot stops two
    card operations overlapping on one terminal inside this process. The key
    stops the SAME operation being carried out twice — the repeat the slot
    cannot see, because the first attempt is no longer running: the manager
    restarted, the request was retried, a second tab was opened.

    Both are taken here, before the caller goes anywhere near the terminal.
    Answers the key that was claimed, or None when the caller sent none — a
    till that sends no key charges exactly as it did before.

    On a refusal nothing stays taken: the slot goes back before the refusal
    leaves, or the next caller would meet a terminal held by a request that
    never ran.
    """
    _claim_terminal(terminal_id, operation)
    claimed = usable_key(key)
    if claimed is None:
        return None
    refusal = _claims(request).begin(
        claimed,
        operation=operation,
        terminal_id=terminal_id,
        amount_kopecks=amount_kopecks,
    )
    if refusal is None:
        return claimed
    _release_terminal(terminal_id)
    _log.warning(
        "[%s] terminal=%s key=...%s refused: this operation is already %s, "
        "and carrying it out again could take the money twice",
        operation, terminal_id, claimed[-8:], refusal["state"],
    )
    raise HTTPException(status_code=409, detail=refusal)


def _settle_claim(
    request: Request,
    key: Optional[str],
    result,
    *,
    operation: str,
    terminal_id: str,
    amount_kopecks: int,
) -> Optional[str]:
    """Finish the key if the terminal said what happened — and not otherwise.

    Answers the claim's state for the caller to carry back, so the till knows
    whether its key is spent or still in doubt. None when no key was sent.

    AN ANSWER THAT DOES NOT SAY WHAT HAPPENED DOES NOT FINISH ANYTHING. The
    adapters turn every outcome they do not recognise into `declined` — a
    timeout, an empty transaction result, a first step read back as if it were
    the last. Settling those as `done` lets the key go, and the cashier can
    then put a second card through for money that may already have moved. So
    the claim is left as it stands and the `finally` below writes it
    `unresolved`: the repeat is refused and named, which costs a manual check.
    """
    if key is None:
        return None
    # `mode="json"` and nothing else: the terminal fills `payment_date` with a
    # datetime whenever it reports a transaction time, and a datetime in here
    # made the claim file unwritable from that moment on (found by review).
    answered = result.model_dump(mode="json") if result is not None else None
    if not says_what_happened(answered):
        _log.error(
            "[%s] terminal=%s key=...%s amount=%s the answer does not say what "
            "happened to the money (status=%s result=%s code=%s): the operation "
            "is left unresolved, and a repeat of this key will be refused "
            "instead of being sent to the terminal again",
            operation, terminal_id, key[-8:], amount_kopecks,
            (answered or {}).get("status"),
            (answered or {}).get("raw_transaction_result"),
            (answered or {}).get("error_code"),
        )
        return STATE_UNRESOLVED
    kept = kept_result(answered)
    _claims(request).settle(key, kept)
    _log.info(
        "[%s] terminal=%s key=...%s amount=%s settled as %s (rrn=%s auth=%s); "
        "a repeat of this key is answered from it",
        operation, terminal_id, key[-8:], amount_kopecks,
        (kept or {}).get("status"), (kept or {}).get("rrn"),
        (kept or {}).get("auth_code"),
    )
    return STATE_DONE


def _free_claim_if_nothing_was_sent(
    request: Request, key: Optional[str], exc: TerminalUnavailable, *, operation: str,
) -> None:
    """Free the key when the failure happened before the card was touched.

    A terminal with no address or no merchant chosen is the commonest way a
    charge fails, and nothing was put to the card: holding the key would mean
    the cashier could not retry the same payment at all after fixing it. Every
    other failure keeps its key — a timeout or a broken connection does not say
    whether the money moved, and a repeat under that key must not charge again.

    TWO WAYS A FAILURE CAN SAY NOTHING WAS SENT, and nothing else counts:

    * its code is one of the few that are raised while the parameters are
      still being assembled (`NOTHING_WAS_SENT_CODES`);
    * the adapter says so on the exception itself — the transport never
      connected, and no frame that could move money had gone out before it
      tried. A pulled cable arrives this way: the code is `unreachable`, which
      is not and must not be on the list, because the same code is raised by a
      socket that died mid-`Purchase`.

    THE DEFAULT IS TO KEEP. The fact is read as true only when it is exactly
    `True`: an adapter that does not set it, an older adapter that has never
    heard of it, anything uncertain — all keep the key, which costs a manual
    check. Read the other way round it would cost a second charge.
    """
    if key is None:
        return
    code = getattr(exc, "code", None)
    code_says_so = code in NOTHING_WAS_SENT_CODES
    adapter_says_so = getattr(exc, "nothing_sent", False) is True
    if not code_says_so and not adapter_says_so:
        _log.warning(
            "[%s] key=...%s stays claimed: the terminal failed with %s and "
            "nothing says the request never left, so the money may have "
            "moved; a repeat of this key will be refused instead of charging "
            "again",
            operation, key[-8:], code,
        )
        return
    _claims(request).release(key)
    _log.info(
        "[%s] key=...%s is free again: refused with %s before anything was "
        "put to the card (%s)",
        operation, key[-8:], code,
        "a refusal raised before the request was built" if code_says_so
        else "the transport never connected, so not a byte of the request "
             "was written",
    )


def _leave_claim_unfinished(request: Request, key: Optional[str]) -> None:
    """Runs in the same `finally` as the journal's row: whatever became of the
    request, a claim this process is no longer holding must not read as one
    somebody is working on."""
    if key is None:
        return
    _claims(request).unfinished(key)


# ---------------------------------------------------------------------------
# Discovery + registration
# ---------------------------------------------------------------------------



def _journal_operation(
    terminal_id: str,
    action: str,
    amount_kopecks: int,
    *,
    result=None,
    error: Optional[str] = None,
) -> None:
    """PET-1087 — one card operation, as the shop will read it: the sum, and
    whether the money moved. Written after the terminal has answered, and the
    journal keeps its own failures to itself — a row is not worth a payment.

    A refusal by the card has its own status, `declined`: it is not a broken
    device, and a shop reading «failed» would go looking for one. Review found
    the first version filing every non-ok answer as a failure, which is the
    very confusion this journal exists to end.
    """
    if result is not None:
        # A `declined` from an adapter is not always the card saying no: ours
        # fold a timeout and an unexpected answer into the same word (ssi:
        # «everything else (decline / error / timeout) → declined»). So the
        # journal asks whether the TERMINAL SPOKE — a response code or the
        # protocol's own verbatim result means it did, and then the refusal is
        # the card's. Without either, nobody refused anything and the row says
        # `failed` (found by the second review round).
        spoke = bool(result.response_code or result.raw_transaction_result)
        status = (
            work_journal.STATUS_OK
            if result.status == "ok"
            else work_journal.STATUS_CANCELLED
            if result.status == "cancelled"
            else work_journal.STATUS_DECLINED
            if result.status == "declined" and spoke
            else work_journal.STATUS_FAILED
        )
        # The bank's code is what anybody sorting this out with the bank will
        # be asked for, so it stays beside the words.
        parts = [p for p in (result.error_message, result.error_code) if p]
        detail = " · ".join(parts) or (None if result.status == "ok" else result.status)
    else:
        status = work_journal.STATUS_FAILED
        detail = error
    work_journal.record_terminal(
        terminal_id, action, status, amount_kopecks=amount_kopecks, detail=detail,
    )


class _Operation:
    """PET-1087 — one row per attempt, whatever happens to the request.

    Review found the holes: `CancelledError` is a BaseException (the cashier's
    browser closes mid-payment) and an `HTTPException` raised inside an adapter
    is re-raised untouched — both left a card operation that may well have
    gone through with no row at all. So the row is written in a `finally`, and
    this little object remembers whether one was written already.
    """

    def __init__(self, terminal_id: str, action: str, amount_kopecks: int) -> None:
        self.terminal_id = terminal_id
        self.action = action
        self.amount_kopecks = amount_kopecks
        self.recorded = False

    def done(self, result) -> None:
        _journal_operation(self.terminal_id, self.action, self.amount_kopecks, result=result)
        self.recorded = True

    def failed(self, error: Optional[str]) -> None:
        _journal_operation(self.terminal_id, self.action, self.amount_kopecks, error=error)
        self.recorded = True

    def unfinished(self) -> None:
        """Nothing answered and nothing was recorded — say exactly that.

        The status is `unknown`, not `failed`: the money may well have moved,
        and «failed» would be a guess that is wrong half the time (second
        review round). The bank's own records settle it, and the row tells the
        shop where to look.
        """
        if self.recorded:
            return
        work_journal.record_terminal(
            self.terminal_id,
            self.action,
            work_journal.STATUS_UNKNOWN,
            amount_kopecks=self.amount_kopecks,
            detail="the request ended without an answer from the terminal",
        )


@router.post("/discover")
async def discover(request: Request) -> dict:
    """LAN-scan + probe. Returns discovered terminals merged with
    manually registered ones so the frontend always sees the full list.

    Bounded by `DASHBOARD_SWEEP_BUDGET_S`, like the remote command is
    by its own budget. The sweep is no longer the cheap thing it was —
    it gained a second, patient pass worth 25–27 s per /24 and some
    80 s across four subnets — and an unbounded sweep behind an HTTP
    request is only as good as the shortest timeout in front of it.
    What could not be covered is said out loud (`partial`) instead of
    being passed off as a complete answer; the second pass is NOT made
    conditional on the first one finding nothing, because a shop with
    one quick terminal and one wedged one is exactly the case the
    patient pass exists for.
    """
    import asyncio
    import logging

    from src.devices.scan import DASHBOARD_SWEEP_BUDGET_S, discover_network_terminals

    log = logging.getLogger("src.routes.terminal")

    registry = _registry(request)
    coverage: dict = {}
    descriptors = await asyncio.to_thread(
        discover_network_terminals,
        budget_s=DASHBOARD_SWEEP_BUDGET_S,
        coverage=coverage,
    )
    registry.remember_descriptors(descriptors)

    # Merge: start with discovered, then add manually registered ones
    # that didn't show up in the scan (different subnet, offline, etc.)
    seen_ids = {d.id for d in descriptors}
    manual = [
        reg.descriptor
        for reg in registry.all_registrations()
        if reg.descriptor.id not in seen_ids
    ]

    body: dict = {"terminals": [d.model_dump() for d in descriptors + manual]}
    if coverage.get("cut_short"):
        # The dashboard reads `terminals` and nothing else, so this is
        # for the log and for whoever reads the API — it is the same
        # answer, with the part that could not be covered named.
        body["partial"] = True
        body["note"] = (
            f"the sweep was kept inside {DASHBOARD_SWEEP_BUDGET_S:.0f}s: "
            f"{coverage['pairs']} address:port pairs over "
            f"{coverage['hosts']} hosts, second patient pass "
            f"{coverage['slow_pass']}, {coverage['probes_skipped']} probe(s) "
            f"not run. A terminal may be missing; add it by address, or "
            f"scan one subnet at a time."
        )
        log.warning(
            "terminal discovery for the dashboard did not cover everything "
            "inside its %.0fs budget: %s pairs over %s hosts, slow pass %s, "
            "%s of %s probes not run (%s of them cut off) — %d terminal(s) "
            "answered",
            DASHBOARD_SWEEP_BUDGET_S, coverage["pairs"], coverage["hosts"],
            coverage["slow_pass"], coverage["probes_skipped"],
            coverage["probes_run"] + coverage["probes_skipped"],
            coverage.get("probes_cut_off", 0), len(descriptors),
        )
    return body


@router.get("")
async def list_registered(request: Request) -> dict:
    return {
        "terminals": [r.model_dump() for r in _registry(request).all_registrations()],
    }


@router.post("/register")
async def register(payload: TerminalRegistrationRequest, request: Request) -> dict:
    try:
        reg = _registry(request).register(payload)
    except UnknownTerminal as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_terminal", "message": str(exc)},
        )
    return {"terminal": reg.model_dump()}


@router.post("/register-manual")
async def register_manual(payload: ManualTerminalRequest, request: Request) -> dict:
    """Skip discovery and register a terminal directly by IP/port.
    Needed when the LAN scan can't reach the terminal (planet's mobile
    hotspot, carrier CGNAT, VLAN). Returns the same shape as
    /terminal/register so the dashboard can refresh in one call."""
    registry = _registry(request)
    descriptor = registry.add_manual_descriptor(
        host=payload.host,
        port=payload.port,
        kind=payload.kind,
        label=payload.label,
    )
    reg_req = TerminalRegistrationRequest(
        id=descriptor.id,
        kind=payload.kind,
        nickname=payload.nickname,
    )
    reg = registry.register(reg_req)
    return {"terminal": reg.model_dump()}


@router.post("/serial-scan")
async def serial_scan(request: Request) -> dict:
    """List available serial/COM ports (USB terminals on Windows show up as a
    virtual COM port). Used by the dashboard's "Add terminal → USB" tab so the
    operator can pick the port. Does NOT touch the LAN scan. No-op off Windows
    / without pyserial → empty list."""
    import asyncio

    from src.devices.scan import list_serial_ports

    ports = await asyncio.to_thread(list_serial_ports)
    return {"ports": [d.model_dump() for d in ports]}


@router.post("/register-serial")
async def register_serial(payload: SerialTerminalRequest, request: Request) -> dict:
    """Register a USB terminal by its virtual COM port. Mirrors
    register-manual, but serial instead of TCP."""
    registry = _registry(request)
    descriptor = registry.add_manual_descriptor_serial(
        port=payload.port,
        baudrate=payload.baudrate,
        kind=payload.kind,
        label=payload.label,
    )
    reg = registry.register(TerminalRegistrationRequest(
        id=descriptor.id,
        kind=payload.kind,
        nickname=payload.nickname,
    ))
    return {"terminal": reg.model_dump()}


@router.delete("/{terminal_id}")
async def unregister(terminal_id: str, request: Request) -> dict:
    try:
        _registry(request).unregister(terminal_id)
    except UnknownTerminal as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_terminal", "message": str(exc)},
        )
    return {"status": "unregistered", "terminal_id": terminal_id}


# ---------------------------------------------------------------------------
# Per-terminal operations
# ---------------------------------------------------------------------------


@router.post("/{terminal_id}/ping")
async def ping(terminal_id: str, request: Request) -> dict:
    adapter, _ = _resolve(request, terminal_id)
    try:
        ok = await adapter.ping()
    except TerminalUnavailable as exc:
        raise _surface_terminal_error(exc, operation="ping", terminal_id=terminal_id)
    return {"ok": ok, "terminal_id": terminal_id}


@router.get("/{terminal_id}/info")
async def info(terminal_id: str, request: Request) -> dict:
    adapter, _ = _resolve(request, terminal_id)
    try:
        return {"terminal_id": terminal_id, "info": await adapter.get_info()}
    except TerminalUnavailable as exc:
        raise _surface_terminal_error(exc, operation="info", terminal_id=terminal_id)


@router.get("/{terminal_id}/merchants")
async def list_merchants(terminal_id: str, request: Request) -> dict:
    """Live merchant roster from the terminal, merged with operator-
    set nicknames the registry has remembered. New bank-side merchants
    show up here on the next reload; removed ones disappear; nicknames
    survive both."""
    adapter, _ = _resolve(request, terminal_id)
    try:
        merchants = await adapter.list_merchants()
    except TerminalUnavailable as exc:
        raise _surface_terminal_error(exc, operation="merchants", terminal_id=terminal_id)
    fresh = [
        MerchantBinding(
            merchant_id=m.merchant_id,
            terminal_id=m.terminal_id,
            merchant_name=m.merchant_name,
        )
        for m in merchants
    ]
    registry = _registry(request)
    try:
        merged = registry.merge_merchant_list(terminal_id, fresh)
    except UnknownTerminal:
        # Caller asked for merchants on an un-registered terminal —
        # still return the live list (useful during the discover/
        # register flow, when the UI peeks before persisting).
        merged = fresh
    return {
        "terminal_id": terminal_id,
        "merchants": [m.model_dump() for m in merged],
    }


@router.put("/{terminal_id}/merchants")
async def update_merchants(
    terminal_id: str, payload: MerchantNicknameUpdate, request: Request,
) -> dict:
    """Bulk-update merchant nicknames. The Settings UI sends the full
    list (operator may rename multiple at once); we replace what's
    stored. Nicknames are what the cashier sees in the merchant
    select at payment time."""
    try:
        reg = _registry(request).update_merchants(terminal_id, payload.merchants)
    except UnknownTerminal as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_terminal", "message": str(exc)},
        )
    return {
        "terminal_id": terminal_id,
        "merchants": [m.model_dump() for m in reg.merchants],
    }


@router.post("/charge")
async def charge(
    payload: ChargeRequest,
    request: Request,
    terminal_id: Optional[str] = None,
) -> dict:
    """Single-call payment: send Purchase, wait for the terminal to
    finish chip/PIN, return the unified AcquirerResult. The frontend's
    NgRx effect awaits this and feeds the result into the fiscal
    receipt flow."""
    import logging
    log = logging.getLogger("src.routes.terminal")
    adapter, used_id = _resolve(request, terminal_id)
    log.info(
        "[charge] terminal=%s payload=%s",
        used_id,
        payload.model_dump_json(),
    )
    # Both guards before the first irreversible step. PET-1304: the key is
    # claimed here, not after the terminal has answered — a refusal issued
    # once the card has been charged refuses nothing.
    key = _claim_operation(
        request,
        operation="charge",
        terminal_id=used_id,
        key=payload.transaction_uid,
        amount_kopecks=payload.amount_kopecks,
    )
    operation = _Operation(used_id, work_journal.ACTION_SALE, payload.amount_kopecks)
    try:
        try:
            result = await adapter.charge(payload)
        except TerminalUnavailable as exc:
            operation.failed(str(exc) or getattr(exc, "code", None))
            _free_claim_if_nothing_was_sent(request, key, exc, operation="charge")
            raise _surface_terminal_error(exc, operation="charge", terminal_id=used_id)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — surface root cause instead of bare 500
            log.exception("[charge] terminal=%s unhandled error: %s", used_id, exc)
            operation.failed(f"{type(exc).__name__}: {exc}")
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "internal_error",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
        log.info("[charge] terminal=%s result=%s", used_id, result.model_dump_json())
        operation.done(result)
        claim_state = _settle_claim(
            request, key, result,
            operation="charge",
            terminal_id=used_id,
            amount_kopecks=payload.amount_kopecks,
        )
        # The till reads this to decide whether its key is spent: an answer
        # that does not say what happened leaves the key in doubt, and the
        # till must keep it rather than mint a new one for the next press.
        return {
            "terminal_id": used_id,
            "result": result.model_dump(),
            "claim_state": claim_state,
        }
    finally:
        # Nothing answered and nothing was recorded: a cancelled request (the
        # cashier's browser closed mid-payment) or an HTTPException raised
        # inside the adapter. The money may have moved, so the attempt is in
        # the journal either way — and the operation's key says so too.
        operation.unfinished()
        _leave_claim_unfinished(request, key)
        _release_terminal(used_id)


@router.post("/refund")
async def refund(
    payload: RefundRequest,
    request: Request,
    terminal_id: Optional[str] = None,
) -> dict:
    """PET-882 — гроші назад на ту саму картку.

    Дзеркало `/charge`: один виклик, чекаємо на остаточну відповідь
    термінала, віддаємо той самий `AcquirerResult`. Каса створює проводку
    повернення й фіскальний чек ЛИШЕ після `status == "ok"` — саме тому тут
    важливо не вдавати успіх: відмова термінала їде назад як відмова, а не
    як порожній результат.

    Який саме операцією повертати, вирішує каса (`choose_refund_operation`)
    і кладе в `extras.operation`; замовчування — `Refund`, бо воно працює
    завжди, коли відомі номери оплати.
    """
    import logging
    log = logging.getLogger("src.routes.terminal")
    adapter, used_id = _resolve(request, terminal_id)
    log.info(
        "[refund] terminal=%s payload=%s", used_id, payload.model_dump_json(),
    )
    # The same two guards as the charge: money leaves the shop here as surely
    # as it arrives there (PET-1304).
    key = _claim_operation(
        request,
        operation="refund",
        terminal_id=used_id,
        key=payload.transaction_uid,
        amount_kopecks=payload.amount_kopecks,
    )
    operation = _Operation(used_id, work_journal.ACTION_REFUND, payload.amount_kopecks)
    try:
        try:
            result = await adapter.refund(payload)
        except TerminalUnavailable as exc:
            operation.failed(str(exc) or getattr(exc, "code", None))
            _free_claim_if_nothing_was_sent(request, key, exc, operation="refund")
            raise _surface_terminal_error(exc, operation="refund", terminal_id=used_id)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — surface root cause instead of bare 500
            log.exception("[refund] terminal=%s unhandled error: %s", used_id, exc)
            operation.failed(f"{type(exc).__name__}: {exc}")
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "internal_error",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
        log.info("[refund] terminal=%s result=%s", used_id, result.model_dump_json())
        operation.done(result)
        claim_state = _settle_claim(
            request, key, result,
            operation="refund",
            terminal_id=used_id,
            amount_kopecks=payload.amount_kopecks,
        )
        return {
            "terminal_id": used_id,
            "result": result.model_dump(),
            "claim_state": claim_state,
        }
    finally:
        # Nothing answered and nothing was recorded: a cancelled request (the
        # cashier's browser closed mid-payment) or an HTTPException raised
        # inside the adapter. The money may have moved, so the attempt is in
        # the journal either way — and the operation's key says so too.
        operation.unfinished()
        _leave_claim_unfinished(request, key)
        _release_terminal(used_id)


@router.post("/{terminal_id}/cancel")
async def cancel(terminal_id: str, request: Request) -> dict:
    """Operator hit Cancel mid-transaction. Only effective in S02/S03/S08
    per SSI doc §5.4.2 — otherwise the terminal ignores it. Adapter
    swallows the error in that case; we always return 200."""
    adapter, _ = _resolve(request, terminal_id)
    await adapter.cancel()
    return {"status": "cancel_requested", "terminal_id": terminal_id}


@router.get("/{terminal_id}/last-result")
async def last_result(
    terminal_id: str,
    request: Request,
    transaction_uid: Optional[str] = None,
) -> dict:
    """Recovery hook — fetch the most recent (or specific-uid) result
    after a network drop mid-charge. Frontend uses this to confirm a
    transaction it thinks succeeded but didn't get the final result
    for."""
    adapter, _ = _resolve(request, terminal_id)
    try:
        result = await adapter.get_last_result(transaction_uid=transaction_uid)
    except TerminalUnavailable as exc:
        raise _surface_terminal_error(exc, operation="last-result", terminal_id=terminal_id)
    return {"terminal_id": terminal_id, "result": result.model_dump()}
