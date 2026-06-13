"""Interactive runner for the SSI terminal emulator.

    python -m emulator                 # listen on 127.0.0.1:3000
    python -m emulator --port 3000 --host 0.0.0.0

A colourful arrow-key menu pops for every Purchase: ↑/↓ to pick
Approve / Decline / Cancel, Enter to confirm. Wire traffic is logged to
``emulator.log`` so the console stays clean.

Local test tool only.
"""

from __future__ import annotations

import argparse
import logging
import queue

import questionary
from questionary import Choice, Style
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .ssi_terminal import Pending, SSITerminalEmulator, start_server_thread

console = Console()

MENU_STYLE = Style(
    [
        ("qmark", "fg:#00afff bold"),
        ("question", "bold"),
        ("pointer", "fg:#00d75f bold"),
        ("highlighted", "fg:#00d75f bold"),
        ("selected", "fg:#00d75f"),
    ]
)

API_KEY = "bf11b47b-e139-4f03-8e02-9c2e692f91b8"  # manager DEFAULT_API_KEY


def _format_amount(amount_kopecks: int, currency: str) -> str:
    symbol = "грн" if currency in ("980", "UAH") else currency
    return f"{amount_kopecks / 100:.2f} {symbol}"


def _banner(host: str, port: int, manager_port: int) -> None:
    register = (
        f"curl -X POST http://127.0.0.1:{manager_port}/terminal/register-manual \\\n"
        f'  -H "X-Api-Key: {API_KEY}" -H "Content-Type: application/json" \\\n'
        f'  -d \'{{"host":"{host}","port":{port},"kind":"mono_pos","nickname":"Емулятор"}}\''
    )
    body = Text()
    body.append("SSI / Mono POS terminal — device side\n\n", style="bold cyan")
    body.append("Listening on  ", style="dim")
    body.append(f"{host}:{port}\n\n", style="bold green")
    body.append("Register it in the manager (once):\n", style="dim")
    body.append(register + "\n\n", style="yellow")
    body.append("Wire traffic → ", style="dim")
    body.append("emulator.log", style="bold")
    console.print(
        Panel(body, title="🏦  BARHANDLER TERMINAL EMULATOR", border_style="cyan")
    )


def _prompt_outcome(pending: Pending) -> str:
    amount = _format_amount(pending.amount_kopecks, pending.currency)
    console.print()
    console.print(
        Panel(
            Text(f"💳  Оплата на {amount}", style="bold white"),
            border_style="magenta",
            title="Нова транзакція",
        )
    )
    choice = questionary.select(
        "Що робить термінал?",
        choices=[
            Choice(title="✅  Approve  (схвалити)", value="a"),
            Choice(title="❌  Decline  (відхилити)", value="d"),
            Choice(title="🚫  Cancel   (скасувати)", value="c"),
        ],
        style=MENU_STYLE,
        qmark="›",
        instruction="(↑/↓, Enter)",
    ).ask()
    # ask() returns None on Ctrl-C — treat as cancel so the manager isn't left
    # hanging on a transaction with no outcome.
    return choice or "c"


def _print_result(decision: str, amount: str) -> None:
    if decision == "a":
        console.print(f"   [bold green]✔ APPROVED[/]  {amount}\n")
    elif decision == "d":
        console.print(f"   [bold red]✘ DECLINED[/]  {amount}\n")
    else:
        console.print(f"   [bold yellow]⊘ CANCELLED[/]  {amount}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="SSI POS terminal emulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--manager-port", type=int, default=9999)
    parser.add_argument("--merchant-id", default="00000012345")
    parser.add_argument("--merchant-name", default="EMULATOR MERCHANT")
    parser.add_argument("--terminal-id", default="EMU00001")
    args = parser.parse_args()

    logging.basicConfig(
        filename="emulator.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    decisions: "queue.Queue[Pending]" = queue.Queue()

    def on_traffic(direction: str, message: dict) -> None:
        logging.getLogger("emulator.ssi").info("%s %s", direction.upper(), message)

    emulator = SSITerminalEmulator(
        decisions=decisions,
        merchant_id=args.merchant_id,
        merchant_name=args.merchant_name,
        terminal_id=args.terminal_id,
        on_traffic=on_traffic,
    )

    start_server_thread(args.host, args.port, emulator)
    _banner(args.host, args.port, args.manager_port)
    console.print("[dim]Чекаю на транзакції… (Ctrl-C — вихід)[/]\n")

    try:
        while True:
            pending = decisions.get()
            decision = _prompt_outcome(pending)
            pending.decision = decision
            pending.event.set()
            _print_result(decision, _format_amount(pending.amount_kopecks, pending.currency))
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Вихід.[/]")


if __name__ == "__main__":
    main()
