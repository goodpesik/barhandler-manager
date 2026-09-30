"""PET-971 — where the offline service's config comes from.

Activation (the shop, the device token, the data key kept by the OS) is
PET-972: until it exists no shop is activated and the service is not started.
A developer can point BHM_OFFLINE_CONFIG_FILE at a JSON file with the config
to run it by hand.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def current_config() -> Optional[dict]:
    """The activated shop's config for the offline service, or None."""
    path = os.environ.get("BHM_OFFLINE_CONFIG_FILE")
    if not path:
        return None
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("offline config %s unreadable: %s", path, e)
        return None
    return cfg if isinstance(cfg, dict) else None
