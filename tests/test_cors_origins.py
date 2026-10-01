"""PET-996 — which pages may call the manager from a browser.

The offline Petshandler till is served from 127.0.0.1:9898 and prints
through this manager; its preflight used to be refused, so nothing printed.
"""

from __future__ import annotations

import pytest


def preflight(client, origin: str):
    return client.options(
        "/printers",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-api-key",
        },
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:9898",
        "http://localhost:9898",
        "https://work.petshandler.com",
    ],
)
def test_our_pages_may_call(client, origin):
    r = preflight(client, origin)
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1.evil.example",
        "https://evil.example",
        "http://127.0.0.2:9898",
    ],
)
def test_other_pages_may_not(client, origin):
    r = preflight(client, origin)
    assert r.headers.get("access-control-allow-origin") != origin
