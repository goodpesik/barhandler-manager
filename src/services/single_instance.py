"""Whether another manager already holds the port (BH-190)."""

from __future__ import annotations

import contextlib
import errno
import logging
import socket

log = logging.getLogger(__name__)

#: The one bind error that means another listener holds the address. Named
#: explicitly: every other error is a probe that could not be judged, and the
#: manager starts anyway rather than leaving the station without one.
_ADDRESS_IN_USE = frozenset(
    {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}
)


def port_taken(host: str, port: int) -> bool:
    """True when the address cannot be bound because something listens on it.

    A bind and not a connect: a manager that is still starting has bound the
    port but may not answer yet. No SO_REUSEADDR, so that an existing listener
    always makes the bind fail.

    A bind that fails for any other reason — a Windows excluded or reserved
    port range (WSAEACCES), an address that is not there, a socket that could
    not be made — says nothing about another copy. It comes back False with
    the reason in the log: the caller exits on True, so reading an unjudgeable
    probe as «taken» would leave the station with no manager at all.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
    except OSError as e:
        if e.errno in _ADDRESS_IN_USE:
            return True
        log.warning(
            "binding %s:%s could not be judged (%s): nothing says another manager copy "
            "holds it, this copy starts",
            host, port, e,
        )
        return False
    finally:
        # Guarded: this is a throwaway bind probe, and a close that raised
        # would come out of the `finally` instead of the answer — the manager
        # would fail to start over a socket nobody needs any more.
        with contextlib.suppress(OSError):
            s.close()
    return False
