"""PET-1087 — /station: who this machine is, what its devices did, and the
device settings the app keeps here.

The point of the settings half: the browser's localStorage is not a place to
keep which printer prints receipts. Clearing the browser data must not leave
the till without its printers while the printers stand registered right here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.constants import DEFAULT_API_KEY


def test_the_station_says_who_it_is(client: TestClient, auth_headers: dict) -> None:
    body = client.get("/station", headers=auth_headers).json()
    assert body["installId"], "the station must have a stable identifier"
    assert "label" in body and "platform" in body and "version" in body
    assert body["journalEntries"] == 0


def test_the_station_is_behind_the_key(client: TestClient) -> None:
    """The identifier of a shop's machine is not for anybody who can reach the
    port."""
    assert client.get("/station").status_code == 401
    assert client.get("/station/journal").status_code == 401
    assert client.post("/station/settings", json={}).status_code == 401


def test_the_journal_is_empty_on_a_fresh_station(client: TestClient, auth_headers: dict) -> None:
    body = client.get("/station/journal", headers=auth_headers).json()
    assert body["entries"] == []
    # Nothing to ask from next time, so the cursor stays where the caller is.
    assert body["pageLastSeq"] == 0


def test_the_journal_answers_the_rows_after_a_number(client: TestClient, auth_headers: dict) -> None:
    journal = client.app.state.work_journal
    journal.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
    journal.record(device_kind="terminal", device_id="t1", action="sale", status="ok", amount_kopecks=5000)

    whole = client.get("/station/journal", headers=auth_headers).json()
    seqs = [e["seq"] for e in whole["entries"]]
    assert len(seqs) == 2 and seqs[0] < seqs[1]
    assert whole["pageLastSeq"] == seqs[-1] == whole["lastSeq"]

    rest = client.get(f"/station/journal?after={seqs[0]}", headers=auth_headers).json()
    assert [e["action"] for e in rest["entries"]] == ["sale"]
    assert rest["entries"][0]["amount"] == 5000
    assert rest["pageLastSeq"] == seqs[1]


def test_a_page_hands_back_its_own_cursor_and_the_stations_highest(
    client: TestClient, auth_headers: dict,
) -> None:
    """Found by review: an app that took the station's highest number as its
    cursor would skip every row between the page and that number."""
    journal = client.app.state.work_journal
    rows = [
        journal.record(device_kind="printer", device_id="p1", action="receipt", status="ok")
        for _ in range(5)
    ]
    seqs = [r["seq"] for r in rows]

    body = client.get("/station/journal?after=0&limit=2", headers=auth_headers).json()
    assert [e["seq"] for e in body["entries"]] == seqs[:2]
    # The cursor for the next call is the page's own last row…
    assert body["pageLastSeq"] == seqs[1]
    # …while the station's highest says how much is still queued.
    assert body["lastSeq"] == seqs[-1]


@pytest.mark.parametrize("query", ["?after=-1", "?limit=0", "?limit=5000"])
def test_the_journal_refuses_a_nonsense_page(client: TestClient, auth_headers: dict, query: str) -> None:
    assert client.get(f"/station/journal{query}", headers=auth_headers).status_code == 422


# ---------- the device settings of this station ----------


SHOP = "11111111-1111-1111-1111-111111111111"


def test_settings_are_kept_and_given_back(client: TestClient, auth_headers: dict) -> None:
    items = {"phm.manager.printers": "[]", "phm.manager.fiscalPrinter": "p1"}
    saved = client.post(
        "/station/settings", headers=auth_headers, json={"appid": SHOP, "items": items},
    ).json()
    assert saved["count"] == 2 and saved["id"] and saved["savedAt"]

    read = client.get(f"/station/settings?appid={SHOP}", headers=auth_headers).json()
    assert read["items"] == items
    assert read["id"] == saved["id"]


def test_each_save_is_a_new_blob(client: TestClient, auth_headers: dict) -> None:
    """The app tells its own copy from a newer one by the id, not by the time:
    a station's clock can jump back."""
    first = client.post(
        "/station/settings", headers=auth_headers, json={"appid": SHOP, "items": {"a": "1"}},
    ).json()
    second = client.post(
        "/station/settings", headers=auth_headers, json={"appid": SHOP, "items": {"a": "2"}},
    ).json()
    assert first["id"] != second["id"]
    assert client.get(f"/station/settings?appid={SHOP}", headers=auth_headers).json()["items"] == {"a": "2"}


def test_another_shop_does_not_read_this_shops_settings(client: TestClient, auth_headers: dict) -> None:
    client.post("/station/settings", headers=auth_headers, json={"appid": SHOP, "items": {"a": "1"}})
    other = client.get("/station/settings?appid=22222222-2222-2222-2222-222222222222", headers=auth_headers)
    assert other.status_code == 404


def test_nothing_saved_yet_is_an_honest_404(client: TestClient, auth_headers: dict) -> None:
    r = client.get(f"/station/settings?appid={SHOP}", headers=auth_headers)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "no_station_settings"


@pytest.mark.parametrize(
    "payload",
    [
        {"items": {"a": "1"}},                      # no shop
        {"appid": SHOP},                            # nothing to save
        {"appid": SHOP, "items": {}},               # the same, spelled out
        {"appid": SHOP, "items": {"a": 1}},         # not a string
        {"appid": SHOP, "items": "everything"},     # not settings at all
    ],
)
def test_a_body_that_is_not_device_settings_is_refused(
    client: TestClient, auth_headers: dict, payload: dict,
) -> None:
    r = client.post("/station/settings", headers=auth_headers, json=payload)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] in {"settings_refused", "bad_request"}


def test_an_oversized_value_is_refused(client: TestClient, auth_headers: dict) -> None:
    r = client.post(
        "/station/settings",
        headers=auth_headers,
        json={"appid": SHOP, "items": {"phm.manager.printers": "x" * (64 * 1024 + 1)}},
    )
    assert r.status_code == 400


def test_the_settings_survive_a_restart_of_the_manager(client: TestClient, auth_headers: dict, config: dict) -> None:
    """The whole reason the station keeps them: a reinstalled browser finds its
    printers again."""
    client.post("/station/settings", headers=auth_headers, json={"appid": SHOP, "items": {"a": "1"}})

    from src.server import create_app

    with TestClient(create_app(config)) as fresh:
        read = fresh.get(f"/station/settings?appid={SHOP}", headers={"X-Api-Key": DEFAULT_API_KEY})
        assert read.status_code == 200
        assert read.json()["items"] == {"a": "1"}


def test_the_shop_being_saved_is_never_the_one_evicted(client: TestClient, auth_headers: dict) -> None:
    """Found by review: the first version evicted by the saved-at time, and a
    station clock that steps back makes the blob just saved the oldest one —
    thrown away inside the very call that answered «saved»."""
    from src.services.station_settings import MAX_SHOPS

    for n in range(MAX_SHOPS):
        client.post(
            "/station/settings", headers=auth_headers, json={"appid": f"shop-{n}", "items": {"a": "1"}},
        )
    # The clock steps back a year for the next save.
    import src.services.station_settings as settings_module
    from datetime import datetime, timedelta, timezone

    real = settings_module.datetime

    class _Stepped:
        @staticmethod
        def now(tz=None):
            return real.now(tz or timezone.utc) - timedelta(days=365)

    settings_module.datetime = _Stepped
    try:
        saved = client.post(
            "/station/settings", headers=auth_headers, json={"appid": "shop-new", "items": {"a": "2"}},
        )
    finally:
        settings_module.datetime = real
    assert saved.status_code == 200

    read = client.get("/station/settings?appid=shop-new", headers=auth_headers)
    assert read.status_code == 200, "the shop that just saved must still be there"
    assert read.json()["items"] == {"a": "2"}
    # And the room was made by forgetting the shop that saved first.
    assert client.get("/station/settings?appid=shop-0", headers=auth_headers).status_code == 404


def test_a_save_that_could_not_be_written_changes_nothing(client: TestClient, auth_headers: dict) -> None:
    """Also from the review: the first version put the new blob into memory
    before writing the file, so a failed write answered 500 while GET already
    served the new settings and the evicted shop was already gone."""
    client.post("/station/settings", headers=auth_headers, json={"appid": SHOP, "items": {"a": "1"}})
    store = client.app.state.station_settings

    def _boom() -> None:
        raise OSError("read-only disk")

    store._save_locked = _boom  # noqa: SLF001
    failed = client.post(
        "/station/settings", headers=auth_headers, json={"appid": SHOP, "items": {"a": "2"}},
    )
    del store._save_locked  # noqa: SLF001
    assert failed.status_code == 500

    read = client.get(f"/station/settings?appid={SHOP}", headers=auth_headers).json()
    assert read["items"] == {"a": "1"}, "what is served must be what the station holds"


def test_a_shop_that_is_not_a_string_is_refused(client: TestClient, auth_headers: dict) -> None:
    r = client.post("/station/settings", headers=auth_headers, json={"appid": ["a"], "items": {"a": "1"}})
    assert r.status_code == 400


def test_an_oversized_key_is_refused(client: TestClient, auth_headers: dict) -> None:
    r = client.post(
        "/station/settings", headers=auth_headers, json={"appid": SHOP, "items": {"k" * 300: "1"}},
    )
    assert r.status_code == 400
