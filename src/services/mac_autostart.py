"""Best-effort LaunchAgent self-install for the frozen macOS .app.

The unsigned .dmg ships no installer script (unlike the curl|bash path or the
Windows Inno setup), so the app registers its own autostart on launch: write
``~/Library/LaunchAgents/com.goodpesik.barhandler-manager.plist`` pointing at
our own executable.

We only WRITE the plist here — we do NOT ``launchctl load`` it now, because
this process already holds port 9999 and a loaded agent would spawn a second
instance that can't bind (KeepAlive would then crash-loop it). launchd starts
it at the next login instead; the double-clicked session serves until then.
This mirrors the Windows install, where autostart also takes effect at logon.
"""

from __future__ import annotations

import logging
import plistlib
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_LABEL = "com.goodpesik.barhandler-manager"


def ensure_launch_agent() -> None:
    """Register a login LaunchAgent for the frozen mac app. No-op elsewhere.

    Idempotent: rewrites the plist only when it's missing or points at a
    different executable (so moving / reinstalling the .app re-targets it).
    Never raises — autostart registration must not block the server starting.
    """
    if not (getattr(sys, "frozen", False) and sys.platform == "darwin"):
        return
    try:
        exe = str(Path(sys.executable).resolve())
        log_dir = Path.home() / ".barhandler-manager"
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist_path = agents / f"{_LABEL}.plist"

        if plist_path.exists():
            try:
                current = plistlib.loads(plist_path.read_bytes())
                if current.get("ProgramArguments") == [exe]:
                    return  # already registered for this exact app
            except Exception:
                pass  # unreadable/legacy → overwrite below

        plist_path.write_bytes(
            plistlib.dumps(
                {
                    "Label": _LABEL,
                    "ProgramArguments": [exe],
                    "RunAtLoad": True,
                    "KeepAlive": True,
                    "StandardOutPath": str(log_dir / "bhm.out.log"),
                    "StandardErrorPath": str(log_dir / "bhm.err.log"),
                }
            )
        )
        logger.info("mac autostart registered: %s -> %s", plist_path, exe)
    except Exception as exc:  # noqa: BLE001 — never block startup on this
        logger.warning("mac autostart self-install failed: %s", exc)
