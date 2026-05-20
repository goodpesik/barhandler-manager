"""SSI ECR JSON adapter — Monobank / PrivatBank / Raiffeisen / Pivdenny.

Wire protocol details (from SSI doc 1.4.1, indexed in MemPalace under
wing=barhandler, room=architecture):

- TCP socket on port 3000, frame layout:
      <STX 02 66 01> <LEN 2B big-endian> <DATA UTF-8 JSON ≤64K> <LRC 1B>
  LRC = XOR of every DATA byte.
- HTTP variant on port 3001 with plain JSON body (no framing) — we
  default to TCP because it's the broadly-supported transport; HTTP
  is opt-in via TerminalNetworkAddress.use_http.
- 15-second per-request timeout, ≥0.25s pause between requests.
- No auth — terminal trusted on LAN.

The protocol's async pattern is "ack-then-poll": every financial
operation returns `{error: false}` immediately and moves to a non-S00
status; the host polls `GetStatus` until S00, then reads the final
outcome via `GetLastResult`. We bake that loop into `charge()` so the
route layer just awaits a single coroutine.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from src.models.terminal import (
    AcquirerResult,
    ChargeRequest,
    TerminalDescriptor,
    TerminalKind,
    TerminalNetworkAddress,
    TerminalRegistration,
    TerminalTransport,
    make_terminal_id,
)
from src.services.terminals.base import (
    MerchantInfo,
    TerminalAdapter,
    TerminalUnavailable,
)

logger = logging.getLogger(__name__)


STX_PREFIX = bytes([0x02, 0x66, 0x01])  # STX + protocol_id + version
DEFAULT_TCP_PORT = 3000
DEFAULT_HTTP_PORT = 3001
REQUEST_TIMEOUT_S = 15.0  # protocol max (sec 1.1)
INTER_REQUEST_PAUSE_S = 0.30  # > 0.25 protocol minimum
STATUS_POLL_INTERVAL_S = 0.5
STATUS_POLL_MAX_S = 180.0  # outer cap: cardholder can take a couple of minutes

# ESC/POS-style outcome mapping. APPROVED-anything → ok; explicit cancel →
# cancelled; everything else (decline / error / timeout) → declined and we
# pass the SSI verbatim value up to the UI so the operator sees specifics.
_OK_RESULTS = {"OK", "APPROVED", "APPROVED_ONLINE", "APPROVED_OFFLINE", "COMPLETED"}
_CANCELLED_RESULTS = {"CANCELLED", "CANCELLED_BEFORE_START"}
_INTERRUPTABLE_STATUSES = {"S02", "S03", "S08"}  # cancel() works in these only
_IDLE_STATUSES = {"S00", "S08"}  # charge complete + ready for next op


# ---------------------------------------------------------------------------
# Frame helpers (TCP transport)
# ---------------------------------------------------------------------------


def calc_lrc(payload: bytes) -> int:
    """XOR every byte of the DATA segment. Doc 6.1 algorithm verbatim."""
    lrc = 0
    for b in payload:
        lrc ^= b
    return lrc


def encode_frame(message: dict) -> bytes:
    """Build a complete TCP frame from a JSON-able dict.

    Doc §1.3 grammar: STX(3) + LEN(2 BE) + DATA + LRC(1). LEN counts
    only the DATA bytes — STX and LEN themselves are excluded.
    """
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(data) > 0xFFFF:
        raise ValueError(f"DATA exceeds 64K limit ({len(data)} bytes)")
    return STX_PREFIX + len(data).to_bytes(2, "big") + data + bytes([calc_lrc(data)])


class FrameError(ValueError):
    """Raised on STX mismatch or LRC failure — typically means the
    other end isn't speaking SSI, or the wire got corrupted."""


def decode_frame(buffer: bytes) -> dict:
    """Decode a complete frame. Raises FrameError on bad STX / LRC."""
    if len(buffer) < 6:
        raise FrameError(f"frame too short: {len(buffer)} bytes")
    if buffer[:3] != STX_PREFIX:
        raise FrameError(f"bad STX prefix: {buffer[:3].hex()}")
    data_len = int.from_bytes(buffer[3:5], "big")
    if len(buffer) < 5 + data_len + 1:
        raise FrameError(
            f"truncated frame: header says {data_len} data bytes, "
            f"got {len(buffer) - 6} after STX+LEN+LRC",
        )
    data = buffer[5 : 5 + data_len]
    received_lrc = buffer[5 + data_len]
    expected_lrc = calc_lrc(data)
    if received_lrc != expected_lrc:
        raise FrameError(
            f"LRC mismatch: got {received_lrc:#04x}, expected {expected_lrc:#04x}",
        )
    return json.loads(data.decode("utf-8"))


async def send_tcp(host: str, port: int, message: dict, timeout: float = REQUEST_TIMEOUT_S) -> dict:
    """Open a TCP connection, send one framed message, read the framed
    response, close. Caller is responsible for the ≥0.25s pause between
    consecutive calls."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise TerminalUnavailable(
            f"cannot connect to {host}:{port}: {exc}",
            code="unreachable",
        ) from exc

    try:
        writer.write(encode_frame(message))
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        # Header first so we know how many DATA bytes to read.
        header = await asyncio.wait_for(reader.readexactly(5), timeout=timeout)
        if header[:3] != STX_PREFIX:
            raise FrameError(f"bad STX in response: {header[:3].hex()}")
        data_len = int.from_bytes(header[3:5], "big")
        rest = await asyncio.wait_for(
            reader.readexactly(data_len + 1),  # +1 for LRC
            timeout=timeout,
        )
        return decode_frame(header + rest)
    except asyncio.TimeoutError as exc:
        raise TerminalUnavailable(
            f"timeout talking to {host}:{port}",
            code="timeout",
        ) from exc
    except (OSError, asyncio.IncompleteReadError) as exc:
        raise TerminalUnavailable(
            f"transport error talking to {host}:{port}: {exc}",
            code="unreachable",
        ) from exc
    except FrameError as exc:
        raise TerminalUnavailable(
            f"malformed SSI response from {host}:{port}: {exc}",
            code="protocol_error",
        ) from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 — close may double-fault
            pass


# ---------------------------------------------------------------------------
# Adapter (probe-only in this commit; methods land in commit 2)
# ---------------------------------------------------------------------------


class SSITerminalAdapter(TerminalAdapter):
    """Implementation of TerminalAdapter for the SSI ECR JSON protocol.

    The class deliberately stays stateless beyond the registration so
    rerouting after a terminal reboot or DHCP change is a no-op — the
    registry rewrites the descriptor and the next charge() picks up
    the new address from `self.descriptor.network`.
    """

    @classmethod
    async def probe(cls, host: str, port: int = DEFAULT_TCP_PORT) -> Optional[TerminalDescriptor]:
        """Send PingDevice; if we get a valid SSI frame back, ask for
        GetTerminalInfo to flesh out the descriptor. On any transport
        or protocol error we return None so the LAN scan can move on
        quietly."""
        try:
            pong = await send_tcp(host, port, {"method": "PingDevice"}, timeout=2.0)
        except (TerminalUnavailable, FrameError):
            return None
        if not isinstance(pong, dict) or pong.get("method") != "PingDevice":
            return None

        # Best-effort: enrich with model + serial. If it fails we still
        # return a usable descriptor — operator can register and refine.
        model: Optional[str] = None
        serial: Optional[str] = None
        kind_hint: Optional[TerminalKind] = None
        try:
            info = await send_tcp(host, port, {"method": "GetTerminalInfo"}, timeout=5.0)
            params = info.get("params", {}) if isinstance(info, dict) else {}
            # Android shape: terminalModel + terminalSerialNumber + currentApp
            model = params.get("terminalModel") or params.get("model")
            serial = params.get("terminalSerialNumber") or params.get("pos_sn")
            current = params.get("currentApp") or {}
            package = (current.get("packageName") or "").lower() if isinstance(current, dict) else ""
            kind_hint = _kind_from_package(package)
        except (TerminalUnavailable, FrameError):
            pass

        terminal_id = make_terminal_id(
            TerminalTransport.network,
            host,
            str(port),
            serial or "",
        )
        label = f"{model or 'SSI POS'} @ {host}"
        if serial:
            label = f"{label} (s/n {serial})"

        return TerminalDescriptor(
            id=terminal_id,
            transport=TerminalTransport.network,
            label=label,
            kind=kind_hint,
            model=model,
            serial=serial,
            network=TerminalNetworkAddress(host=host, port=port),
        )

    # The remaining adapter methods (ping/get_info/list_merchants/charge/
    # cancel/get_last_result) land in commit 2 to keep this PR review-able.

    async def ping(self) -> bool:  # pragma: no cover — implemented in commit 2
        raise NotImplementedError

    async def get_info(self) -> dict:  # pragma: no cover
        raise NotImplementedError

    async def list_merchants(self) -> list[MerchantInfo]:  # pragma: no cover
        raise NotImplementedError

    async def charge(self, request: ChargeRequest) -> AcquirerResult:  # pragma: no cover
        raise NotImplementedError

    async def cancel(self) -> None:  # pragma: no cover
        raise NotImplementedError

    async def get_last_result(
        self, transaction_uid: Optional[str] = None,
    ) -> AcquirerResult:  # pragma: no cover
        raise NotImplementedError


def _kind_from_package(package_name: str) -> Optional[TerminalKind]:
    """Heuristic: which bank does the active payment app belong to.

    Used during probe to label discovered terminals so the operator
    doesn't have to guess. Falls through to `generic_ssi` for unknown
    packages.
    """
    if "monobank" in package_name:
        return TerminalKind.mono_pos
    if "privat" in package_name:
        return TerminalKind.privat_pos
    if "raif" in package_name:
        return TerminalKind.raif_pos
    if "pivdenny" in package_name:
        return TerminalKind.pivdenny_pos
    return None


__all__ = [
    "SSITerminalAdapter",
    "FrameError",
    "calc_lrc",
    "encode_frame",
    "decode_frame",
    "send_tcp",
    "STX_PREFIX",
    "DEFAULT_TCP_PORT",
    "REQUEST_TIMEOUT_S",
    "INTER_REQUEST_PAUSE_S",
]
