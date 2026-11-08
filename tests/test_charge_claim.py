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


def test_a_claim_too_old_to_be_a_repeat_stops_standing_in_the_way(
    tmp_path: Path,
) -> None:
    """A key is remembered for as long as a repeat of it can arrive, and no
    longer: a guard that never lets go of a key would be its own defect, and
    the file would grow without end."""
    claims = _store(tmp_path, retention_s=1)
    claims.begin("k-4", operation="charge", terminal_id="abc", amount_kopecks=100)
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
