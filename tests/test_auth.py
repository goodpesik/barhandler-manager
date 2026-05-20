"""Auth surface — every protected route must reject missing/bad keys."""

from fastapi.testclient import TestClient


def test_health_is_public(client: TestClient) -> None:
    # /health is the only unauthenticated route — frontends use it to
    # show a status pill without needing the key.
    assert client.get("/health").status_code == 200


def test_devices_requires_key(client: TestClient) -> None:
    assert client.get("/devices").status_code == 401


def test_devices_rejects_wrong_key(client: TestClient) -> None:
    response = client.get("/devices", headers={"X-Api-Key": "definitely-not"})
    assert response.status_code == 401


def test_devices_accepts_real_key(client: TestClient, auth_headers: dict) -> None:
    response = client.get("/devices", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {"printers": []}
