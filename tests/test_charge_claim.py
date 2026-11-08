"""PET-1304 — the store of claimed card-operation keys.

The route-level behaviour (a repeat refused without a second charge, a till
that sends no key charging as before) lives in `test_terminal_routes.py`.
What is here is the part of the guard the route cannot show: what a claim
becomes when the process that held it is gone, and the two ways a claim stops
standing in the way of a legitimate charge.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from src.services.charge_claim import (
    CODE_ALREADY_DONE,
    CODE_UNRESOLVED,
    STATE_IN_FLIGHT,
    STATE_UNRESOLVED,
    ChargeClaims,
    kept_result,
    says_what_happened,
)


def _store(tmp_path: Path, **kwargs) -> ChargeClaims:
    return ChargeClaims(path=tmp_path / "charge-claims.json", **kwargs)


def test_an_operation_in_flight_when_the_manager_stopped_is_read_as_unresolved(
    tmp_path: Path,
) -> None:
    """The restart in the middle of a charge.

    The claim on disk says `in_flight`, and nobody can be running it — this
    process has only just started. Read back as `in_flight` it would tell the
    cashier to wait for an operation that is not happening; read as
    `unresolved` it says what is true, and refuses the repeat either way.
    """
    (tmp_path / "charge-claims.json").write_text(
        json.dumps({
            "claims": {
                "k-1": {
                    "state": STATE_IN_FLIGHT,
                    "at": time.time(),
                    "operation": "charge",
                    "terminal_id": "abc",
                    "amount": 24500,
                },
            },
        }),
        encoding="utf-8",
    )
    claims = _store(tmp_path)
    claims.load()

    assert claims.state_of("k-1") == STATE_UNRESOLVED
    refusal = claims.begin(
        "k-1", operation="charge", terminal_id="abc", amount_kopecks=24500,
    )
    assert refusal is not None
    assert refusal["code"] == CODE_UNRESOLVED


def test_a_key_released_before_anything_was_sent_can_be_charged_again(
    tmp_path: Path,
) -> None:
    """A charge refused for a missing merchant put nothing to the card, so the
    cashier must be able to retry the same payment once it is picked."""
    claims = _store(tmp_path)
    assert claims.begin(
        "k-2", operation="charge", terminal_id="abc", amount_kopecks=100,
    ) is None
    claims.release("k-2")

    assert claims.state_of("k-2") is None
    assert claims.begin(
        "k-2", operation="charge", terminal_id="abc", amount_kopecks=100,
    ) is None


def test_a_settled_key_answers_the_repeat_with_what_the_terminal_said(
    tmp_path: Path,
) -> None:
    """The answer is kept with the key: a repeat is told the operation is done
    and given its result, instead of the money being taken a second time."""
    claims = _store(tmp_path)
    claims.begin("k-3", operation="charge", terminal_id="abc", amount_kopecks=100)
    claims.settle("k-3", {"status": "ok", "rrn": "9999999999"})

    refusal = claims.begin(
        "k-3", operation="charge", terminal_id="abc", amount_kopecks=100,
    )
    assert refusal is not None
    assert refusal["code"] == CODE_ALREADY_DONE
    assert refusal["result"]["rrn"] == "9999999999"


def test_a_settled_claim_too_old_to_be_a_repeat_stops_standing_in_the_way(
    tmp_path: Path,
) -> None:
    """A SETTLED key is remembered for as long as a repeat of it can arrive,
    and no longer: a guard that never lets go would be its own defect, and the
    file would grow without end. An unsettled one is a different matter — see
    below."""
    claims = _store(tmp_path, retention_s=1)
    claims.begin("k-4", operation="charge", terminal_id="abc", amount_kopecks=100)
    claims.settle("k-4", {"status": "ok"})
    # Age the claim rather than waiting out the retention — the test says
    # «older than the window», which is what the store decides on.
    claims._claims["k-4"]["at"] = time.time() - 5

    assert claims.begin(
        "k-4", operation="charge", terminal_id="abc", amount_kopecks=100,
    ) is None


def test_a_claim_this_process_is_no_longer_holding_is_not_left_in_flight(
    tmp_path: Path,
) -> None:
    """The request ended with no answer — a timeout, an unhandled error, a
    closed browser. The next caller must be told the operation was never
    finished, not that somebody is carrying it out."""
    claims = _store(tmp_path)
    claims.begin("k-5", operation="charge", terminal_id="abc", amount_kopecks=100)
    claims.unfinished("k-5")

    assert claims.state_of("k-5") == STATE_UNRESOLVED


def test_an_answer_does_not_become_unfinished_afterwards(tmp_path: Path) -> None:
    """`unfinished` runs in the route's `finally`, after the answer has been
    recorded on a charge that went through — it must leave a settled key
    alone, or every successful charge would end up looking unresolved."""
    claims = _store(tmp_path)
    claims.begin("k-6", operation="charge", terminal_id="abc", amount_kopecks=100)
    claims.settle("k-6", {"status": "ok"})
    claims.unfinished("k-6")

    refusal = claims.begin(
        "k-6", operation="charge", terminal_id="abc", amount_kopecks=100,
    )
    assert refusal is not None
    assert refusal["code"] == CODE_ALREADY_DONE


def test_an_answer_that_could_not_be_stored_does_not_block_the_next_claim(
    tmp_path: Path,
) -> None:
    """A row json cannot take must not take the file with it.

    This is how the guard failed: a settled answer carried a value json could
    not write, every save from then on raised, and the claim of the NEXT card
    operation never reached the disk — so a restart lost it and the repeat it
    was taken for would have charged the card again.
    """
    claims = _store(tmp_path)
    claims.begin("k-7", operation="charge", terminal_id="abc", amount_kopecks=100)
    claims.settle("k-7", {"status": "ok", "rrn": object()})

    claims.begin("k-8", operation="charge", terminal_id="abc", amount_kopecks=100)

    on_disk = json.loads((tmp_path / "charge-claims.json").read_text(encoding="utf-8"))
    assert "k-8" in on_disk["claims"]
    assert on_disk["claims"]["k-8"]["state"] == STATE_IN_FLIGHT


def test_a_claim_file_that_is_not_an_object_does_not_stop_the_manager(
    tmp_path: Path,
) -> None:
    """A station that will not start takes its till with it, which is worse
    than losing the record of the keys. Valid JSON that is not an object is
    read as no claims at all."""
    for content in ("null", "[]", '"x"', '{"claims": []}'):
        (tmp_path / "charge-claims.json").write_text(content, encoding="utf-8")
        claims = _store(tmp_path)
        claims.load()
        assert claims.count == 0


def test_an_unsettled_key_is_never_dropped_to_make_room(tmp_path: Path) -> None:
    """Dropping a key that was started and never answered for re-admits it,
    and the repeat goes to the terminal: the money may already have moved.
    Only a key the terminal answered for may be pruned."""
    claims = _store(tmp_path, limit=1)
    claims.begin("k-9", operation="charge", terminal_id="abc", amount_kopecks=100)
    claims.begin("k-10", operation="charge", terminal_id="abc", amount_kopecks=100)

    refusal = claims.begin(
        "k-9", operation="charge", terminal_id="abc", amount_kopecks=100,
    )
    assert refusal is not None
    assert refusal["state"] == STATE_IN_FLIGHT


def test_an_unsettled_key_is_never_dropped_for_being_old(tmp_path: Path) -> None:
    """The same rule by age: a day passing does not establish what happened to
    the money, and the answer is still only on the terminal."""
    claims = _store(tmp_path, retention_s=1)
    claims.begin("k-11", operation="charge", terminal_id="abc", amount_kopecks=100)
    claims._claims["k-11"]["at"] = time.time() - 5

    refusal = claims.begin(
        "k-11", operation="charge", terminal_id="abc", amount_kopecks=100,
    )
    assert refusal is not None
    assert refusal["state"] == STATE_IN_FLIGHT


def test_only_an_answer_that_names_the_outcome_may_finish_a_claim() -> None:
    """The adapters map everything they do not recognise onto `declined`, so
    `declined` alone cannot mean "nothing was taken". A named refusal may
    finish the claim; an unnamed one is the absence of an answer."""
    assert says_what_happened({"status": "ok"})
    assert says_what_happened({"status": "cancelled"})
    assert says_what_happened({"status": "declined", "response_code": "05"})
    assert says_what_happened({"status": "declined", "error_code": "E11"})
    assert says_what_happened(
        {"status": "declined", "raw_transaction_result": "DECLINED_ONLINE"},
    )

    # The money may have moved and nothing here says so.
    assert not says_what_happened({"status": "declined"})
    assert not says_what_happened(
        {"status": "declined", "raw_transaction_result": "FIRST_STEP_COMPLETED"},
    )
    assert not says_what_happened(
        {"status": "declined", "raw_transaction_result": "SOMETHING_NEW"},
    )
    assert not says_what_happened({"status": "pending"})
    assert not says_what_happened(None)


def test_a_settled_answer_keeps_what_the_till_reads_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The file sits on the shop's disk for a day. It holds the acquiring
    block the till puts on the receipt, and not the terminal's whole reply."""
    claims = _store(tmp_path)
    claims.begin("k-12", operation="charge", terminal_id="abc", amount_kopecks=100)
    claims.settle("k-12", kept_result({
        "status": "ok",
        "rrn": "9999999999",
        "auth_code": "123456",
        "cardmask": "444455******1111",
        "payment_date": "2026-11-08T10:00:00",
        "terminal_receipt": "the cardholder slip",
        "invoice_num": "000123",
        "vendor_data": {"pan": "4444551111111111"},
    }))

    stored = json.loads(
        (tmp_path / "charge-claims.json").read_text(encoding="utf-8"),
    )["claims"]["k-12"]["result"]
    assert stored["rrn"] == "9999999999"
    assert stored["auth_code"] == "123456"
    for gone in ("payment_date", "terminal_receipt", "invoice_num", "vendor_data"):
        assert gone not in stored
