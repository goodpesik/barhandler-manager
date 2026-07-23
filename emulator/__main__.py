"""Interactive multi-bank POS-terminal emulator for barhandler-manager.

    python -m emulator                    # pick a bank from a menu
    python -m emulator --kind raif_pos    # start straight on one bank
    python -m emulator --host 0.0.0.0     # listen on all interfaces

Pick which bank/terminal to emulate at startup. While idle you can switch to
another bank without restarting (the old listener is torn down and the new
protocol comes up on its own port). Every Purchase pops an arrow-key menu:
Approve / Decline / Cancel.

Supported: Monobank & generic SSI, PrivatBank, Raiffeisen/PUMB (Printec
PosAPI), Bank Pivdenny/Sense (BPOS), Oschadbank. Local test tool only —
not imported by the manager app.
"""

from __future__ import annotations

import argparse
import logging
import queue
from dataclasses import dataclass
from typing import Callable, Optional

import questionary
from questionary import Choice, Style
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .base_terminal import BankEmulator, EmulatorServer, Pending
from .bpos_terminal import BposTerminalEmulator
from .oschad_terminal import OschadTerminalEmulator
from .posapi_terminal import PosApiTerminalEmulator
from .privat_terminal import PrivatTerminalEmulator
from .ssi_terminal import SSITerminalEmulator

console = Console()

MENU_STYLE = Style([
    ("qmark", "fg:#00afff bold"),
    ("question", "bold"),
    ("pointer", "fg:#00d75f bold"),
    ("highlighted", "fg:#00d75f bold"),
    ("selected", "fg:#00d75f"),
])

API_KEY = "bf11b47b-e139-4f03-8e02-9c2e692f91b8"  # manager DEFAULT_API_KEY


@dataclass(frozen=True)
class Bank:
    key: str            # manager TerminalKind value (register-manual `kind`)
    label: str          # menu label
    protocol: str       # ssi | privat | posapi | bpos | oschad
    port: int           # default listen port
    build: Callable[..., BankEmulator]


def _ssi(package: str, model: str):
    def factory(**kw) -> BankEmulator:
        return SSITerminalEmulator(package=package, model=model, **kw)
    return factory


def _simple(cls, model: str):
    def factory(**kw) -> BankEmulator:
        return cls(model=model, **kw)
    return factory


# Menu order groups by protocol. `key` doubles as the manager `kind` so the
# register-manual hint is always correct for the chosen bank.
BANKS: list[Bank] = [
    Bank("mono_pos", "Monobank  (SSI)", "ssi", 3000,
         _ssi("com.monobank.acquiring", "PAX A50")),
    Bank("generic_ssi", "Generic SSI", "ssi", 3000,
         _ssi("com.ssi.pos", "Verifone X990")),
    Bank("privat_pos", "PrivatBank  (JSON)", "privat", 2000,
         _simple(PrivatTerminalEmulator, "Ingenico Desk 5000")),
    Bank("raif_pos", "Raiffeisen  (Printec PosAPI)", "posapi", 8080,
         _simple(PosApiTerminalEmulator, "Verifone X990")),
    Bank("pumb_pos", "PUMB  (Printec PosAPI)", "posapi", 8080,
         _simple(PosApiTerminalEmulator, "Verifone X990")),
    Bank("pivdenny_pos", "Bank Pivdenny  (BPOS1)", "bpos", 8888,
         _simple(BposTerminalEmulator, "Ingenico Move 5000")),
    Bank("sense_pos", "Sense / Alfa  (BPOS Light)", "bpos", 8888,
         _simple(BposTerminalEmulator, "Ingenico Move 2500")),
    Bank("oschad_pos", "Oschadbank  (ECR)", "oschad", 7777,
         _simple(OschadTerminalEmulator, "PAX A930")),
]
BANKS_BY_KEY = {b.key: b for b in BANKS}


def _format_amount(amount_kopecks: int, currency: str) -> str:
    symbol = "грн" if currency in ("980", "UAH") else currency
    return f"{amount_kopecks / 100:.2f} {symbol}"


def _select_bank(current: Optional[Bank] = None) -> Optional[Bank]:
    choices = []
    for b in BANKS:
        title = f"{'● ' if b is current else '  '}{b.label}  [:{b.port}]"
        choices.append(Choice(title=title, value=b.key))
    answer = questionary.select(
        "Який термінал емулювати?",
        choices=choices, style=MENU_STYLE, qmark="🏦",
        instruction="(↑/↓, Enter)",
    ).ask()
    return BANKS_BY_KEY.get(answer) if answer else None


def _banner(bank: Bank, host: str, port: int, manager_port: int) -> None:
    register = (
        f"curl -X POST http://127.0.0.1:{manager_port}/terminal/register-manual \\\n"
        f'  -H "X-Api-Key: {API_KEY}" -H "Content-Type: application/json" \\\n'
        f'  -d \'{{"host":"{host}","port":{port},"kind":"{bank.key}","nickname":"Емулятор"}}\''
    )
    body = Text()
    body.append(f"{bank.label}\n", style="bold cyan")
    body.append(f"protocol {bank.protocol}\n\n", style="dim")
    body.append("Listening on  ", style="dim")
    body.append(f"{host}:{port}\n\n", style="bold green")
    body.append("Register it in the manager (once):\n", style="dim")
    body.append(register + "\n\n", style="yellow")
    body.append("Wire traffic → ", style="dim")
    body.append("emulator.log", style="bold")
    console.print(Panel(body, title="🏦  BARHANDLER TERMINAL EMULATOR", border_style="cyan"))


def _prompt_outcome(pending: Pending) -> str:
    amount = _format_amount(pending.amount_kopecks, pending.currency)
    console.print()
    console.print(Panel(
        Text(f"💳  Оплата на {amount}", style="bold white"),
        border_style="magenta", title="Нова транзакція",
    ))
    choice = questionary.select(
        "Що робить термінал?",
        choices=[
            Choice(title="✅  Approve  (схвалити)", value="a"),
            Choice(title="❌  Decline  (відхилити)", value="d"),
            Choice(title="🚫  Cancel   (скасувати)", value="c"),
        ],
        style=MENU_STYLE, qmark="›", instruction="(↑/↓, Enter)",
    ).ask()
    return choice or "c"


def _print_result(decision: str, amount: str) -> None:
    if decision == "a":
        console.print(f"   [bold green]✔ APPROVED[/]  {amount}\n")
    elif decision == "d":
        console.print(f"   [bold red]✘ DECLINED[/]  {amount}\n")
    else:
        console.print(f"   [bold yellow]⊘ CANCELLED[/]  {amount}\n")


def _wait_for_transactions(decisions: "queue.Queue[Pending]") -> None:
    """Serve Purchases until the operator presses Ctrl-C to return to the menu."""
    console.print("[dim]Чекаю на транзакції… (Ctrl-C — назад у меню)[/]\n")
    try:
        while True:
            pending = decisions.get()
            decision = _prompt_outcome(pending)
            pending.decision = decision
            pending.event.set()
            _print_result(decision, _format_amount(pending.amount_kopecks, pending.currency))
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]↩ повернення в меню[/]\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-bank POS terminal emulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None,
                        help="override the bank's default port (all banks)")
    parser.add_argument("--kind", choices=list(BANKS_BY_KEY), default=None,
                        help="start straight on this bank instead of the menu")
    parser.add_argument("--manager-port", type=int, default=9999)
    parser.add_argument("--merchant-id", default="00000012345")
    parser.add_argument("--merchant-name", default="EMULATOR MERCHANT")
    parser.add_argument("--terminal-id", default="EMU00001")
    args = parser.parse_args()

    logging.basicConfig(
        filename="emulator.log", level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    decisions: "queue.Queue[Pending]" = queue.Queue()

    def on_traffic(direction: str, message: dict) -> None:
        logging.getLogger("emulator").info("%s %s", direction.upper(), message)

    selected: Optional[Bank] = BANKS_BY_KEY.get(args.kind) if args.kind else _select_bank()
    if selected is None:
        console.print("[dim]Нічого не вибрано — вихід.[/]")
        return

    server: Optional[EmulatorServer] = None
    running_bank: Optional[Bank] = None
    try:
        while True:
            if selected is not running_bank:
                if server is not None:
                    server.stop()
                emulator = selected.build(
                    decisions=decisions,
                    merchant_id=args.merchant_id,
                    merchant_name=args.merchant_name,
                    terminal_id=args.terminal_id,
                    on_traffic=on_traffic,
                )
                port = args.port or selected.port
                server = EmulatorServer(args.host, port, emulator)
                server.start()
                running_bank = selected
                _banner(selected, args.host, port, args.manager_port)

            action = questionary.select(
                "Режим очікування:",
                choices=[
                    Choice(title="⏳  Чекати на транзакції", value="wait"),
                    Choice(title="🔀  Змінити термінал", value="switch"),
                    Choice(title="⏻   Вихід", value="quit"),
                ],
                style=MENU_STYLE, qmark="›", instruction="(↑/↓, Enter)",
            ).ask()

            if action == "wait":
                _wait_for_transactions(decisions)
            elif action == "switch":
                picked = _select_bank(current=running_bank)
                if picked is not None:
                    selected = picked
            else:  # quit or Esc/Ctrl-C on the menu
                break
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        if server is not None:
            server.stop()
        console.print("\n[dim]Вихід.[/]")


if __name__ == "__main__":
    main()
