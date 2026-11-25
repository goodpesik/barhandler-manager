"""Shared secret the logs server checks in the uplink handshake.

Release builds get it from the `UPLINK_SECRET` GitHub secret:
`scripts/write_uplink_secret.py` writes `src/_uplink_secret.py` (git-ignored)
before packaging. It is never committed. A build without it still connects:
the logs server accepts such a handshake while it is in transition mode,
and this module says once that the secret is missing.
"""

from __future__ import annotations

import functools
import logging


@functools.lru_cache(maxsize=1)
def uplink_secret() -> str:
    try:
        from src._uplink_secret import UPLINK_SECRET  # generated at build time
    except ImportError:
        UPLINK_SECRET = ""
    secret = str(UPLINK_SECRET or "").strip()
    if not secret:
        logging.getLogger("uplink").warning(
            "uplink: this build carries no uplink secret; the logs server "
            "accepts the handshake only while it does not require one"
        )
    return secret
