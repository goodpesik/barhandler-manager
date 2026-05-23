"""Dashboard route serves the operator HTML at GET /."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.constants import DEFAULT_API_KEY


def test_root_returns_dashboard_html(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    # Sanity check that the template actually rendered (caught a stale
    # __API_KEY__ placeholder bug during early development).
    assert "<table" in body
    assert "barhandler-manager" in body
    assert "POS-термінали" in body


def test_root_embeds_api_key_for_js_fetch(client: TestClient) -> None:
    """The JS in the page calls the gated /devices + /terminal routes
    with X-Api-Key. The key is substituted into the page at render
    time — verify the placeholder is gone and the real key is there."""
    body = client.get("/").text
    assert "__API_KEY__" not in body
    assert DEFAULT_API_KEY in body


def test_root_does_not_require_api_key(client: TestClient) -> None:
    """Dashboard is unauthenticated by design — operator hits
    http://localhost:9999 in a browser. The handshake key is for the
    JSON endpoints the page itself calls, not the page."""
    # No X-Api-Key header.
    response = client.get("/")
    assert response.status_code == 200
