"""BH-190 — a second manager copy must see that the port is already held."""

from __future__ import annotations

import errno
import logging
import socket

import pytest

from src.services.single_instance import port_taken


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_a_listening_port_is_taken() -> None:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn does
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        assert port_taken("127.0.0.1", listener.getsockname()[1]) is True
    finally:
        listener.close()


def test_a_free_port_is_not_taken() -> None:
    port = _free_port()
    assert port_taken("127.0.0.1", port) is False
    # The check itself must not keep the port: the real manager binds it next.
    assert port_taken("127.0.0.1", port) is False


@pytest.mark.parametrize("code", [errno.EACCES, errno.EADDRNOTAVAIL, 10013])
def test_a_bind_that_cannot_be_judged_does_not_stop_the_start(monkeypatch, caplog, code) -> None:
    """Only «someone listens here» exits; an unjudgeable probe must not."""
    def refused(self, address):
        raise OSError(code, "refused")

    monkeypatch.setattr(socket.socket, "bind", refused)
    with caplog.at_level(logging.WARNING):
        assert port_taken("127.0.0.1", 59999) is False
    assert any("could not be judged" in m for m in caplog.messages), caplog.messages


def test_an_address_already_in_use_still_stops_the_start(monkeypatch) -> None:
    def in_use(self, address):
        raise OSError(errno.EADDRINUSE, "in use")

    monkeypatch.setattr(socket.socket, "bind", in_use)
    assert port_taken("127.0.0.1", 59999) is True
