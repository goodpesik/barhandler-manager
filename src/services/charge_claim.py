"""PET-1304 — one key per card operation, and the repeat it refuses.

The per-terminal slot in `src/routes/terminal.py` serialises card operations
inside ONE manager process: it is a set in memory, and it is gone the moment
the process is. Two things it therefore does not cover.

* A MANAGER RESTART IN THE MIDDLE OF A CHARGE. The till's request dies with
  the process, the card may well have been charged, and the cashier — shown a
  failure — presses Pay again. The new process knows nothing of the first
  attempt and sends a second Purchase.
* ANY REPEAT THAT DOES NOT COME THROUGH THE SAME PROCESS'S MEMORY: a request
  retried by a proxy, a second browser tab, a till reloaded mid-payment.

So the till now mints a key together with the intent to charge and sends it as
`transaction_uid`, and that key is CLAIMED HERE before the terminal is asked
anything. A repeat of a claimed key is refused, and the refusal says which of
the three states it met:

* `in_flight` — this very process is running that operation now.
* `unresolved` — a claim was taken and never settled. Either the holder died
  (the state every `in_flight` record on disk is read back as: nobody can be
  running it, the process is new) or the terminal failed in a way that does
  not say whether the money moved. Both mean: do not charge again, the bank's
  own records settle it.
* `done` — the terminal answered. The answer is kept beside the key, so a
  repeat is answered from it instead of taking the money twice.

WHAT THIS DOES NOT CLAIM TO DO. The file belongs to one machine. Two tills
each running their own manager against one terminal share no state here, and
nothing in this module can make them: what stands between them is the
terminal, which refuses a second ECR connection, and the uid itself, which now
reaches the terminal and lets the bank recognise a repeat.

THE CLAIM IS A GUARD, NOT BOOKKEEPING, so it is taken BEFORE the terminal
call — a refusal issued after the card has been charged is not a refusal of
anything. But a claim that cannot be written to disk does not refuse the sale:
it is kept in memory with a warning. A shop whose disk is full would otherwise
stop taking cards altogether, which is worse than the defect this guards.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("charge-claims.json")

#: A claim is taken and the terminal has not answered yet, in THIS process.
STATE_IN_FLIGHT = "in_flight"
#: A claim nobody is holding and nothing settled — see the module docstring.
STATE_UNRESOLVED = "unresolved"
#: The terminal answered; the answer is kept with the key.
STATE_DONE = "done"

#: The refusal codes the till branches on.
CODE_IN_FLIGHT = "charge_in_flight"
CODE_UNRESOLVED = "charge_unresolved"
CODE_ALREADY_DONE = "charge_already_done"

_REFUSAL_CODE = {
    STATE_IN_FLIGHT: CODE_IN_FLIGHT,
    STATE_UNRESOLVED: CODE_UNRESOLVED,
    STATE_DONE: CODE_ALREADY_DONE,
}

_REFUSAL_MESSAGE = {
    STATE_IN_FLIGHT: (
        "this operation is already running on the terminal; wait for it to "
        "finish instead of starting it again"
    ),
    STATE_UNRESOLVED: (
        "this operation was started and never finished; the money may have "
        "moved — check the terminal before charging again"
    ),
    STATE_DONE: (
        "this operation has already been carried out on the terminal; its "
        "answer is in `result`"
    ),
}

#: How long a settled key is remembered. A till forgets its own key as soon as
#: the terminal answers, so anything arriving under an old key is a repeat of
#: something long finished; a day is far more than the minutes in which a
#: repeat can realistically appear, and it keeps the file small.
DEFAULT_RETENTION_S = 24 * 3600

#: How many keys the file keeps. A till charges a few hundred cards a day, and
#: the retention above prunes long before this; the cap exists so a clock that
#: jumps backwards cannot grow the file without end.
DEFAULT_LIMIT = 2000

#: A key longer than this is not ours — the till's is a uuid. Kept bounded so
#: a caller cannot write an arbitrarily large file through this field.
KEY_MAX_LEN = 128


def usable_key(key: Optional[str]) -> Optional[str]:
    """The key to claim by, or None when the caller sent none we can use.

    No key means the old behaviour, unchanged: the BarHandler till and any
    Petshandler build older than this one send none, and they must keep
    charging exactly as they did.
    """
    if not isinstance(key, str):
        return None
    trimmed = key.strip()
    if not trimmed or len(trimmed) > KEY_MAX_LEN:
        return None
    return trimmed


class ChargeClaims:
    """The keys of the card operations this station has started."""

    def __init__(
        self,
        path: Path = DEFAULT_PATH,
        retention_s: int = DEFAULT_RETENTION_S,
        limit: int = DEFAULT_LIMIT,
    ) -> None:
        self.path = Path(path)
        self.retention_s = max(1, int(retention_s))
        self.limit = max(1, int(limit))
        self._claims: dict[str, dict] = {}
        # Claims are taken on the event loop and the file is written there
        # too, but the journal beside this one is written from device threads
        # as well; one lock costs nothing and makes the pair behave alike.
        self._lock = threading.Lock()

    # ---------- persistence ----------

    def load(self) -> None:
        """Read the file, and read every in-flight claim as unresolved.

        A record saying `in_flight` can only have been written by a process
        that is no longer running — this one has just started. Leaving it as
        `in_flight` would tell the cashier to wait for an operation nobody is
        carrying out; `unresolved` says the thing that is true, that it was
        started and never finished.
        """
        rows: dict[str, dict] = {}
        raised = 0
        if self.path.exists():
            try:
                raw = self.path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("charge-claims could not be read: %s", exc)
                raw = ""
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except Exception:  # noqa: BLE001 — a torn file is not a crash
                logger.warning(
                    "charge-claims could not be understood and is started afresh",
                )
                parsed = {}
            for key, row in (parsed.get("claims") or {}).items():
                if not self._is_row(key, row):
                    continue
                if row["state"] == STATE_IN_FLIGHT:
                    row["state"] = STATE_UNRESOLVED
                    raised += 1
                rows[key] = row
        with self._lock:
            self._claims = rows
            self._prune_locked()
            kept = len(self._claims)
        if raised:
            logger.warning(
                "charge-claims: %s operation(s) were in flight when the manager "
                "last stopped and are read as unresolved — the money may have "
                "moved and a repeat of those keys will be refused",
                raised,
            )
        logger.info("charge-claims: %s key(s) remembered", kept)

    @staticmethod
    def _is_row(key: object, row: object) -> bool:
        return (
            isinstance(key, str)
            and bool(key)
            and isinstance(row, dict)
            and row.get("state") in _REFUSAL_CODE
            and isinstance(row.get("at"), (int, float))
        )

    def _prune_locked(self) -> None:
        """Drop what is too old to be a repeat of anything, and cap the rest."""
        cutoff = time.time() - self.retention_s
        for key in [k for k, row in self._claims.items() if row["at"] < cutoff]:
            del self._claims[key]
        if len(self._claims) > self.limit:
            # Oldest first — the newest keys are the ones a repeat can still
            # arrive for.
            for key, _ in sorted(
                self._claims.items(), key=lambda kv: kv[1]["at"],
            )[: len(self._claims) - self.limit]:
                del self._claims[key]

    def _save_locked(self) -> None:
        """Write the file whole, through a temporary beside it.

        A crash mid-write leaves the previous file intact — the same way the
        registries and the journal are written.
        """
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"claims": self._claims}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def _write_locked(self, what: str) -> None:
        """Persist, or say out loud that this claim lives in memory only."""
        try:
            self._save_locked()
        except Exception as exc:  # noqa: BLE001 — see the module docstring
            logger.warning(
                "charge-claims: %s was not written to disk and is held in "
                "memory only, so it will not survive a restart: %s",
                what, exc,
            )

    # ---------- the guard ----------

    def begin(
        self,
        key: str,
        *,
        operation: str,
        terminal_id: str,
        amount_kopecks: int,
    ) -> Optional[dict]:
        """Take the key, or answer with the refusal of a repeat.

        None means the caller may go on to the terminal. A dict is the
        `detail` of a 409: the code says which state the key was met in, and a
        settled key brings its own answer back with it.
        """
        now = time.time()
        with self._lock:
            self._prune_locked()
            held = self._claims.get(key)
            if held is not None:
                state = held["state"]
                refusal = {
                    "code": _REFUSAL_CODE[state],
                    "message": _REFUSAL_MESSAGE[state],
                    "state": state,
                    "operation": held.get("operation"),
                    "terminal_id": held.get("terminal_id"),
                }
                if held.get("result") is not None:
                    refusal["result"] = held["result"]
                return refusal
            self._claims[key] = {
                "state": STATE_IN_FLIGHT,
                "at": now,
                "operation": operation,
                "terminal_id": terminal_id,
                "amount": int(amount_kopecks),
            }
            self._write_locked("the claim of a card operation")
        return None

    def settle(self, key: str, result: Optional[dict] = None) -> None:
        """The terminal answered: the key is done, and its answer stays with it."""
        with self._lock:
            held = self._claims.get(key)
            if held is None:
                # Nothing to settle — the key was released or pruned under us.
                # Writing it back as `done` would make a key nobody claimed
                # look like a finished operation.
                logger.warning(
                    "charge-claims: the answer of an operation arrived for a "
                    "key that is no longer claimed, and is not recorded",
                )
                return
            held["state"] = STATE_DONE
            held["at"] = time.time()
            held["result"] = result
            self._write_locked("the answer of a card operation")

    def unfinished(self, key: str) -> None:
        """The request ended and nothing answered — the claim is unresolved.

        Every way out of a card operation that is not an answer passes here:
        a terminal that failed in a way which does not say whether the money
        moved, an unhandled error, the cashier's browser closed mid-payment.
        Leaving the claim `in_flight` would tell the next caller to wait for an
        operation nobody is carrying out; `unresolved` says what is true, and
        refuses the repeat just the same.

        A claim that is already settled, or already gone, is left alone.
        """
        with self._lock:
            held = self._claims.get(key)
            if held is None or held["state"] != STATE_IN_FLIGHT:
                return
            held["state"] = STATE_UNRESOLVED
            held["at"] = time.time()
            self._write_locked("an operation left unresolved")

    def release(self, key: str) -> None:
        """Nothing was put to the terminal: the key is free to be used again.

        Only for refusals that happen BEFORE a card operation is sent — a
        terminal with no address, no merchant chosen, a refund asked without
        the numbers it needs. Anything that may have reached the card keeps
        its claim.
        """
        with self._lock:
            if self._claims.pop(key, None) is None:
                return
            self._write_locked("the release of a claim")

    # ---------- reading ----------

    def state_of(self, key: str) -> Optional[str]:
        with self._lock:
            held = self._claims.get(key)
            return held["state"] if held else None

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._claims)
