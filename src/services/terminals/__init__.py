"""POS terminal adapters.

One adapter per wire protocol. Today only `ssi` (which Monobank,
PrivatBank, Raiffeisen and Pivdennybank all share via Servus Systems
Integration). Adding a vendor with a different protocol = new module
implementing the `TerminalAdapter` ABC and registering itself in the
factory in `base.py`.
"""

from src.services.terminals.base import TerminalAdapter, TerminalUnavailable
from src.services.terminals.ssi import SSITerminalAdapter

__all__ = ["TerminalAdapter", "TerminalUnavailable", "SSITerminalAdapter"]
