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
from typing import Optional

from src.models.terminal import (
    AcquirerResult,
    ChargeRequest,
    TerminalDescriptor,
    TerminalRegistration,
)


class TerminalUnavailable(RuntimeError):
    """Raised when the terminal can't be reached or returned a fatal
    protocol error. `code` carries the structured tag that the route
    layer turns into a 503 response with `detail.code` for the frontend
    to branch on (`unreachable`, `out_of_paper`, `cover_open`, ...)."""

    def __init__(self, message: str, code: str = "unavailable") -> None:
        super().__init__(message)
        self.code = code


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
