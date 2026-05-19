"""Custom text → bytes encoders for thermal printers.

Background: most cheap ESC/POS printers from Ukraine ship with a code
page numbered 17 that the firmware calls "PC866" but extended with
Ukrainian-specific letters in the 0xF0–0xF7 range (Ґґ Єє Іі Її). Python's
stdlib `cp866` codec is the Russian variant only — encoding "і" through
it gives "?". This module fills the gap.

Usage:
  esc._raw(b"\\x1bt\\x11")          # tell printer: use table 17 (CP866)
  esc._raw(encode_ua_cp866(text))   # emit text in CP866 + UA overlay
"""

from __future__ import annotations


# Map every Ukrainian letter the python cp866 codec drops to its real
# CP866 (Ukrainian variant) byte. Everything else falls through to the
# standard cp866 encoder.
_UA_OVERLAY = {
    "Ґ": 0xF0,
    "ґ": 0xF1,
    "Є": 0xF2,
    "є": 0xF3,
    "І": 0xF4,
    "і": 0xF5,
    "Ї": 0xF6,
    "ї": 0xF7,
    # Belarusian friends sometimes appear in headers — keep them too.
    "Ў": 0xF8,
    "ў": 0xF9,
    # Em dash / en dash → ASCII hyphen so they don't print as "?".
    "—": 0x2D,
    "–": 0x2D,
    "«": 0x22,
    "»": 0x22,
    "“": 0x22,
    "”": 0x22,
    "‘": 0x27,
    "’": 0x27,
    "№": ord("N"),  # printable fallback
}


def encode_ua_cp866(text: str) -> bytes:
    """Encode `text` using CP866 (Ukrainian) byte map.

    Characters that cp866 doesn't know but our overlay does get the
    overlay byte. Anything else still untranslatable falls back to '?',
    matching the standard `errors='replace'` behaviour.
    """
    out = bytearray()
    for ch in text:
        if ch in _UA_OVERLAY:
            out.append(_UA_OVERLAY[ch])
            continue
        try:
            out.extend(ch.encode("cp866"))
        except UnicodeEncodeError:
            out.append(ord("?"))
    return bytes(out)
