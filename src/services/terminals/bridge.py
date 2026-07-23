"""Shared TCP request/response plumbing for bridge-style terminal adapters.

Unlike SSI (Mono) and PrivatBank — which speak directly to the terminal on
the LAN — Raiffeisen/PUMB (Printec PosAPI), Pivdenny/Sense (BPOS) and
Oschadbank expose their ECR integration through a small **bridge program**
the operator runs next to the manager (Verifone PosAPI service / BPOS
ActiveX host / bank ECR driver). Over the wire that bridge is a plain
TCP JSON endpoint — the only thing that differs per bank is the *framing*
(how one message is delimited) and the field vocabulary.

`exchange()` factors out the identical part — connect, write one framed
request, read exactly one framed response, close, map every failure onto
`TerminalUnavailable` with a stable `.code`. Each adapter supplies two
tiny callables:

    encode(dict) -> bytes            # dict → full wire frame
    read_frame(reader, timeout) -> bytes   # read one frame, return JSON bytes

so the adapter keeps only its bank-specific codec + field mapping.

Note: adapters open a fresh connection per call (no long-lived sockets),
same as ssi.py / privatbank.py — keeps state-tracking trivial and avoids
zombie sockets when the bridge or terminal restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from src.services.terminals.base import TerminalUnavailable

logger = logging.getLogger(__name__)

MAX_FRAME_SIZE = 64 * 1024  # JSON payload guard, matches SSI / PB envelopes
DEFAULT_TIMEOUT_S = 120.0   # cardholder chip+PIN flows can take a minute+


class BridgeFrameError(ValueError):
    """Malformed frame — corrupt JSON, missing delimiter, oversized payload.
    Surfaces as TerminalUnavailable(code="protocol_error") at the transport."""


def json_bytes(message: dict) -> bytes:
    """Compact UTF-8 JSON — no spaces, matching how strict bridge parsers
    (esp. ActiveX/Windows hosts) expect their input."""
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME_SIZE:
        raise ValueError(f"bridge frame exceeds 64K limit ({len(data)} bytes)")
    return data


def parse_json(payload: bytes) -> dict:
    if not payload:
        raise BridgeFrameError("empty frame payload")
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeFrameError(f"invalid JSON in frame: {exc}") from exc
    if not isinstance(parsed, dict):
        raise BridgeFrameError(f"frame is not a JSON object: {type(parsed).__name__}")
    return parsed


async def exchange(
    host: str,
    port: int,
    request: dict,
    *,
    encode: Callable[[dict], bytes],
    read_frame: Callable[[asyncio.StreamReader, float], Awaitable[bytes]],
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Connect → send one framed request → read one framed response → close.

    `encode` turns the request dict into the full wire frame. `read_frame`
    consumes exactly one response frame and returns its JSON payload bytes
    (delimiters stripped). Returns the decoded response dict.

    Raises TerminalUnavailable with `.code` in
    {unreachable, timeout, protocol_error} — never a raw socket error.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=min(timeout, 5.0),
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise TerminalUnavailable(
            f"cannot connect to bridge {host}:{port}: {exc}", code="unreachable",
        ) from exc

    try:
        writer.write(encode(request))
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        payload = await read_frame(reader, timeout)
        return parse_json(payload)
    except asyncio.TimeoutError as exc:
        raise TerminalUnavailable(
            f"timeout talking to bridge {host}:{port}", code="timeout",
        ) from exc
    except (OSError, asyncio.IncompleteReadError) as exc:
        raise TerminalUnavailable(
            f"transport error talking to bridge {host}:{port}: {exc}",
            code="unreachable",
        ) from exc
    except BridgeFrameError as exc:
        raise TerminalUnavailable(
            f"malformed response from bridge {host}:{port}: {exc}",
            code="protocol_error",
        ) from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 — close may double-fault
            pass


__all__ = [
    "BridgeFrameError",
    "MAX_FRAME_SIZE",
    "DEFAULT_TIMEOUT_S",
    "json_bytes",
    "parse_json",
    "exchange",
]
