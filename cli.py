"""Small operator console for barhandler-manager.

What it does:
    status            live dashboard (polls /health, /devices, /terminal)
    start             detached launch (process survives this shell closing)
    stop              kill the detached process by PID file
    restart           stop + start
    logs              tail the manager's log file
    health            one-shot health check, exits non-zero on failure

Why a separate CLI:
    `python main.py` runs the web server inline and dies when the
    shell is closed. The `start` subcommand here spawns the same
    server but in its own session (POSIX `start_new_session=True`),
    redirects stdio to `bhm.log`, and writes the child PID to
    `bhm.pid` so we can stop/restart it later. The dashboard is a
    read-only viewer over the HTTP API — runs whether the manager was
    started by this CLI, by a launchd plist, by systemd, or by hand.

For production (auto-restart on crash + survive reboot + survive user
logout) the install script's launchd/systemd unit is still the right
answer. This CLI is for development and ad-hoc operator use.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent
PIDFILE = ROOT / "bhm.pid"
LOGFILE = ROOT / "bhm.log"
VENV_PY = ROOT / ".venv" / "bin" / "python"
DEFAULT_URL = "http://localhost:9999"

# Token shipped with the manager — same constant as
# `src/constants.py::DEFAULT_API_KEY`. Hard-coded here so the CLI works
# on a fresh checkout without dragging the FastAPI bootstrap path in.
DEFAULT_API_KEY = "bf11b47b-e139-4f03-8e02-9c2e692f91b8"

console = Console()


# --- lifecycle ----------------------------------------------------------------


def read_pid() -> Optional[int]:
    if not PIDFILE.exists():
        return None
    try:
        pid = int(PIDFILE.read_text().strip())
    except (ValueError, OSError):
        return None
    # Stale pidfile (process already gone) — clean up to avoid
    # confusing the next start.
    try:
        os.kill(pid, 0)
    except OSError:
        PIDFILE.unlink(missing_ok=True)
        return None
    return pid


def cmd_start(args: argparse.Namespace) -> int:
    existing = read_pid()
    if existing is not None:
        console.print(f"[yellow]Already running (PID {existing}).[/]")
        return 0
    py = str(VENV_PY) if VENV_PY.exists() else sys.executable
    log = open(LOGFILE, "ab", buffering=0)
    # `start_new_session=True` is the POSIX equivalent of nohup —
    # gives the child its own process group so it survives the shell
    # closing, terminal hang-up, and tab close.
    proc = subprocess.Popen(
        [py, str(ROOT / "main.py")],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    PIDFILE.write_text(str(proc.pid))
    console.print(f"[green]Started.[/] PID {proc.pid}, logs → {LOGFILE}")
    # Give it a moment to bind the port so a follow-up `status` finds it.
    if _wait_for_health(timeout=5.0):
        console.print(f"[green]Healthy on {DEFAULT_URL}[/]")
    else:
        console.print(
            "[yellow]No health response after 5s — check logs:[/] "
            f"`tail -f {LOGFILE}`"
        )
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    pid = read_pid()
    if pid is None:
        console.print("[yellow]Not running.[/]")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        console.print(f"[red]kill failed: {exc}[/]")
        return 1
    # Wait briefly for the process to exit cleanly before reporting.
    # If it's stuck, escalate to SIGKILL — better than leaving a
    # zombie holding port 9999.
    for _ in range(20):  # 2 seconds total
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    PIDFILE.unlink(missing_ok=True)
    console.print(f"[green]Stopped[/] (PID {pid}).")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    cmd_stop(args)
    return cmd_start(args)


def cmd_logs(_args: argparse.Namespace) -> int:
    if not LOGFILE.exists():
        console.print(f"[yellow]No log yet at {LOGFILE}[/]")
        return 0
    # Hand off to `tail -F` — the CLI itself doesn't need to be a log
    # viewer when the OS already has one that does the right thing
    # (handles log rotation, Ctrl+C, etc.).
    os.execvp("tail", ["tail", "-F", str(LOGFILE)])


def cmd_health(_args: argparse.Namespace) -> int:
    try:
        r = httpx.get(f"{DEFAULT_URL}/health", timeout=3.0)
        r.raise_for_status()
        console.print(r.json())
        return 0
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]unreachable: {exc}[/]")
        return 1


# --- dashboard ----------------------------------------------------------------


def _wait_for_health(timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{DEFAULT_URL}/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    return False


def _fetch(path: str, headers: Optional[dict] = None) -> Optional[dict]:
    try:
        r = httpx.get(f"{DEFAULT_URL}{path}", headers=headers, timeout=2.0)
        if r.status_code == 200:
            return r.json()
    except Exception:  # noqa: BLE001
        return None
    return None


def _build_dashboard() -> Panel:
    """Snapshot of /health + /devices + /terminal as one Panel of
    three sub-tables. Called by the Live loop every refresh."""
    auth = {"X-Api-Key": DEFAULT_API_KEY}
    health = _fetch("/health")
    devices = _fetch("/devices", headers=auth) if health else None
    terminals = _fetch("/terminal", headers=auth) if health else None

    if not health:
        return Panel(
            Text("manager unreachable on " + DEFAULT_URL, style="bold red"),
            title="barhandler-manager",
            border_style="red",
        )

    pid = read_pid()
    header = Text()
    header.append("●  ", style="bold green")
    header.append(f"{DEFAULT_URL}", style="bold")
    header.append(f"   v{health.get('version', '?')}")
    if pid:
        header.append(f"   PID {pid}")

    # ---- printers ----
    printers_table = Table(
        title="Printers", title_style="bold cyan",
        header_style="bold", show_lines=False, padding=(0, 1),
    )
    printers_table.add_column("ID", style="dim", no_wrap=True)
    printers_table.add_column("Nickname")
    printers_table.add_column("Role")
    printers_table.add_column("Transport")
    printers_table.add_column("Status")
    health_printers = {p["id"]: p for p in (health.get("printers") or [])}
    for reg in (devices or {}).get("printers", []) or []:
        d = reg.get("descriptor", {})
        pid_ = d.get("id", "")
        status_raw = (health_printers.get(pid_) or {}).get("status", "unknown")
        status_styled = (
            f"[green]{status_raw}[/]"
            if status_raw == "connected"
            else f"[red]{status_raw}[/]"
        )
        printers_table.add_row(
            pid_[:12],
            reg.get("nickname") or d.get("label", ""),
            reg.get("kind", ""),
            d.get("transport", ""),
            status_styled,
        )
    if printers_table.row_count == 0:
        printers_table.add_row("[dim]— none registered —[/]", "", "", "", "")

    # ---- terminals ----
    terminals_table = Table(
        title="POS terminals", title_style="bold cyan",
        header_style="bold", show_lines=False, padding=(0, 1),
    )
    terminals_table.add_column("ID", style="dim", no_wrap=True)
    terminals_table.add_column("Nickname")
    terminals_table.add_column("Kind")
    terminals_table.add_column("Address")
    terminals_table.add_column("Default merchant")
    for reg in (terminals or {}).get("terminals", []) or []:
        d = reg.get("descriptor", {})
        net = d.get("network") or {}
        addr = f"{net.get('host', '?')}:{net.get('port', '?')}" if net else d.get("transport", "")
        terminals_table.add_row(
            d.get("id", "")[:12],
            reg.get("nickname") or d.get("label", ""),
            reg.get("kind", ""),
            addr,
            reg.get("default_merchant_id") or "[dim]—[/]",
        )
    if terminals_table.row_count == 0:
        terminals_table.add_row("[dim]— none registered —[/]", "", "", "", "")

    footer = Text("press Ctrl+C to exit", style="dim")
    return Panel(
        Group(header, Text(""), printers_table, Text(""), terminals_table, Text(""), footer),
        title="barhandler-manager",
        border_style="green",
    )


def cmd_status(_args: argparse.Namespace) -> int:
    try:
        with Live(_build_dashboard(), console=console, refresh_per_second=1) as live:
            while True:
                time.sleep(2.0)
                live.update(_build_dashboard())
    except KeyboardInterrupt:
        console.print("[dim]bye[/]")
        return 0


# --- entry --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="bhm",
        description="barhandler-manager operator CLI",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="Live dashboard (default)")
    sub.add_parser("start", help="Detached start; survives shell closing")
    sub.add_parser("stop", help="Stop the detached process")
    sub.add_parser("restart", help="Stop + start")
    sub.add_parser("logs", help="Tail bhm.log")
    sub.add_parser("health", help="One-shot health check (exit code)")

    args = parser.parse_args()
    cmd = args.cmd or "status"
    handler = {
        "status": cmd_status,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "logs": cmd_logs,
        "health": cmd_health,
    }[cmd]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
