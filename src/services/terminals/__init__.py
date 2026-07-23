"""POS terminal adapters — one per ECR wire protocol.

  - ssi        SSI ECR JSON (Servus) — Monobank + any SSI-firmware unit
  - privatbank PrivatBank ECR JSON — Ingenico/PAX/NEWLAND
  - posapi     Verifone Printec PosAPI — Raiffeisen / PUMB (via bridge)
  - bpos       BPOS1 / BPOS Light — Bank Pivdenny / Sense (via bridge)
  - oschad     Oschadbank ECR (via bridge)

Adding a vendor with a different protocol = new module implementing the
`TerminalAdapter` ABC, an entry in `_ADAPTER_FOR_KIND`
(`src/devices/terminal_registry.py`), and a discovery probe/port in
`src/devices/scan.py`. The route layer is protocol-agnostic.
"""

from src.services.terminals.base import TerminalAdapter, TerminalUnavailable
from src.services.terminals.bpos import BposTerminalAdapter
from src.services.terminals.oschad import OschadTerminalAdapter
from src.services.terminals.posapi import PosApiTerminalAdapter
from src.services.terminals.privatbank import PrivatBankTerminalAdapter
from src.services.terminals.ssi import SSITerminalAdapter

__all__ = [
    "TerminalAdapter",
    "TerminalUnavailable",
    "SSITerminalAdapter",
    "PrivatBankTerminalAdapter",
    "PosApiTerminalAdapter",
    "BposTerminalAdapter",
    "OschadTerminalAdapter",
]
