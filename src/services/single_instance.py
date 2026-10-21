"""Whether another manager already holds the port (BH-190)."""

from __future__ import annotations

import socket


def port_taken(host: str, port: int) -> bool:
    """True when the address cannot be bound because something listens on it.

    A bind and not a connect: a manager that is still starting has bound the
    port but may not answer yet. No SO_REUSEADDR, so that an existing listener
    always makes the bind fail.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
    except OSError:
        return True
    finally:
        s.close()
    return False
