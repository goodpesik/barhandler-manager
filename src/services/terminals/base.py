"""Abstract POS terminal adapter.

Concrete adapters speak one vendor's wire protocol (SSI ECR JSON for
Mono/Privat/Raif/Pivdenny; future Vchasno or other vendors get their
own classes). The route layer never instantiates a concrete adapter
directly — it asks the registry for an adapter that matches the
registered terminal's `TerminalKind`.

Methods are all async because the underlying transports
(asyncio.open_connection for SSI TCP, aiohttp for SSI HTTP) are
async-native, and we want a polled `status()` loop to share the event
loop with the rest of the FastAPI app.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Optional

from src.models.terminal import (
    AcquirerResult,
    ChargeRequest,
    RefundRequest,
    TerminalDescriptor,
    TerminalRegistration,
)


class TerminalUnavailable(RuntimeError):
    """Raised when the terminal can't be reached or returned a fatal
    protocol error. `code` carries the structured tag that the route
    layer turns into a 503 response with `detail.code` for the frontend
    to branch on (`unreachable`, `out_of_paper`, `cover_open`, ...).

    `nothing_sent` is the one fact the code cannot carry: whether a single
    byte of the request had reached the socket when this failure happened.
    PET-1304 — the route frees the operation's idempotency key on it, so it
    DEFAULTS TO FALSE and every raise site that does not set it reads as "the
    money may have moved". Only a transport that knows no byte was written
    sets it, and only an adapter that knows no money-moving frame has gone out
    in this operation lets it travel (see `MoneyFrameTrace`).
    """

    def __init__(
        self, message: str, code: str = "unavailable", *, nothing_sent: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.nothing_sent = nothing_sent is True


#: PET-1304 — the refusals that happen BEFORE anything is put to the card.
#:
#: A terminal with no address, no merchant to charge through, a refund asked
#: without the numbers the operation needs: every one of these is raised while
#: the parameters are being assembled, before the Purchase/Refund frame is
#: written. They are named here, where a new code is added, because the route
#: layer frees an operation's idempotency key for exactly these and keeps it
#: for everything else — a failure that may have reached the card must not let
#: a repeat charge again.
#:
#: Written as the list of what IS safe, never as a list of what is not: a code
#: added later and forgotten by this list keeps its key, which costs a manual
#: retry; the other way round it would cost a second charge.
#:
#: THE CODES THAT LOOK AS IF THEY BELONG HERE AND DO NOT, each checked at its
#: raise site. None of them can be told apart BY ITS CODE from the same code
#: raised once the card had been read, so none of them is added here:
#:
#: * `unreachable` — `ssi.send_tcp` raises it both for a connection that was
#:   refused and for a socket that died AFTER the frame was written, and the
#:   same function carries `Purchase` step 2, the step that moves the money.
#: * `timeout` — the same function on a read that never came, and `_wait_idle`
#:   while waiting for step 2 to finish.
#: * `protocol_error` — a malformed frame, including the reply to a `Purchase`.
#: * every code taken from the terminal's own reply (`charge_rejected`,
#:   `charge_step2_rejected`, `result_fetch_failed`, the raw SSI `eNN`, the
#:   BPOS / Oschad / PosAPI result codes) — the terminal answered a command we
#:   had already sent it.
#:
#: A refused cable is the commonest card failure, and the fact that tells it
#: apart from a socket that died mid-`Purchase` does not live in the code: it
#: lives on the exception, as `TerminalUnavailable.nothing_sent`, set by the
#: transport that knows whether a byte was written and allowed up through the
#: adapter only while no money-moving frame has gone out (`MoneyFrameTrace`).
#: The route frees a key for a code on this list OR for that fact; this list is
#: not widened to carry it.
NOTHING_WAS_SENT_CODES: frozenset[str] = frozenset({
    "not_configured",
    "missing_merchant",
    "merchant_lookup_failed",
    "refund_unsupported",
    "bad_refund_operation",
    "missing_refund_reference",
    "missing_invoice_num",
})


class MoneyFrameTrace:
    """Has a byte of a frame that can move money left, in THIS operation?

    PET-1304. One card operation opens several connections: the Purchase
    itself, the status polls, the result fetch. A transport can only say
    whether the connection IT opened wrote anything, and that is not the
    question the idempotency key turns on — the connect for the status poll of
    a Purchase that has already gone out wrote no byte either, and freeing the
    key there is a second charge for money that has moved.

    So the operation keeps the record. It starts false, it only ever goes true,
    and once true it clears the transport's `nothing_sent` on anything the
    operation raises from then on. A partial write counts as a send: once the
    write of a money-moving frame has been attempted, the only thing that
    still keeps the fact is the transport saying it never connected at all.
    """

    __slots__ = ("money_may_have_left",)

    def __init__(self) -> None:
        self.money_may_have_left = False

    async def send(
        self,
        send_frame: Callable[[dict], Awaitable[dict]],
        message: dict,
    ) -> dict:
        """Send a frame that can move money, and record what became of it."""
        try:
            response = await send_frame(message)
        except TerminalUnavailable as exc:
            if not exc.nothing_sent:
                self.money_may_have_left = True
            raise
        except BaseException:
            # Nothing told us no byte was written, so a byte was written.
            # A cancelled request lands here too, and a cancelled Purchase is
            # exactly the case where the terminal may be reading a card.
            self.money_may_have_left = True
            raise
        self.money_may_have_left = True
        return response

    def seal(self, exc: TerminalUnavailable) -> None:
        """Leave a failure carrying the fact that is true of the OPERATION.

        A transport that never connected says `nothing_sent`, and that is the
        operation's fact only while no money-moving frame has gone out. After
        one has, it is cleared here and the key is kept.
        """
        if self.money_may_have_left:
            exc.nothing_sent = False


class MerchantInfo:
    """Lightweight DTO returned by `list_merchants()` — keeps the
    adapter return type stable even as SSI doc rev adds optional
    fields like bankProfileId."""

    __slots__ = ("merchant_id", "terminal_id", "merchant_name")

    def __init__(
        self,
        merchant_id: str,
        terminal_id: Optional[str] = None,
        merchant_name: Optional[str] = None,
    ) -> None:
        self.merchant_id = merchant_id
        self.terminal_id = terminal_id
        self.merchant_name = merchant_name

    def to_dict(self) -> dict:
        return {
            "merchant_id": self.merchant_id,
            "terminal_id": self.terminal_id,
            "merchant_name": self.merchant_name,
        }


class TerminalAdapter(ABC):
    """One adapter per (wire protocol × transport variant).

    Concrete subclasses receive the descriptor at construction so they
    know the host/port/transport. They DON'T hold long-lived sockets —
    SSI is request/response per call, so each method opens a short
    connection, does its work, closes. Keeps state-tracking trivial
    and avoids zombie-connection issues when the terminal reboots.
    """

    def __init__(self, registration: TerminalRegistration) -> None:
        self.registration = registration
        self.descriptor = registration.descriptor

    @classmethod
    @abstractmethod
    async def probe(cls, host: str, port: int) -> Optional[TerminalDescriptor]:
        """Best-effort identification — used during LAN discovery.

        Returns a `TerminalDescriptor` populated with whatever the
        terminal told us (model, serial, kind heuristic from
        `paymentApps` / `currentApp`). `None` means "host doesn't speak
        our protocol, skip". Must not raise — discovery wants a clean
        bool decision per host.
        """

    @abstractmethod
    async def ping(self) -> bool:
        """Quick liveness check — `PingDevice` in SSI. Returns False on
        any failure, never raises."""

    @abstractmethod
    async def get_info(self) -> dict:
        """Full GetTerminalInfo response (Android XOR Linux shape).
        Raises `TerminalUnavailable` on transport error."""

    @abstractmethod
    async def list_merchants(self) -> list[MerchantInfo]:
        """`GetMerchantListDetailed` — UI uses this to present a select.
        Single-merchant terminals return a one-element list which the
        frontend renders as a disabled select pre-selected."""

    @abstractmethod
    async def charge(self, request: ChargeRequest) -> AcquirerResult:
        """End-to-end Purchase: send request, poll status until S00,
        retrieve final result via GetLastResult. Returns a normalised
        AcquirerResult; the caller doesn't need to know about SSI."""

    async def refund(self, request: RefundRequest) -> AcquirerResult:
        """PET-882 — гроші назад на ту саму картку.

        НЕ абстрактний навмисно. Адаптерів кілька, і повернення вміє поки
        що не кожен банк; зробити метод обовʼязковим означало б одразу
        зламати всі інші адаптери або нашвидкуруч дописати їм заглушки,
        які мовчки вдають, що щось повернули.

        Замість цього замовчування чесно каже «не вмію»: сервер на це
        відповість касиру «проведіть повернення на терміналі вручну» — той
        самий шлях, яким усе працювало досі.
        """
        raise TerminalUnavailable(
            "this terminal cannot refund from the cash register",
            code="refund_unsupported",
        )

    @abstractmethod
    async def cancel(self) -> None:
        """`Interrupt` — only effective in S02/S03/S08. Errors are
        swallowed (already-finished operations are a no-op semantically)."""

    @abstractmethod
    async def get_last_result(
        self, transaction_uid: Optional[str] = None,
    ) -> AcquirerResult:
        """Recovery path after a network drop mid-charge. If
        `transaction_uid` is provided, uses GetResultByUid; otherwise
        GetLastResult."""
