"""Whitelist of diagnostic commands the central log server may invoke
on this manager via socket. Every command is registered explicitly;
unknown commands return `{ok: false, error: "unknown cmd"}` without ever
touching subprocess.

Args are validated per-command. No shell invocations — `subprocess` calls
always use `shell=False` and explicit args arrays. Host-like fields are
filtered through `_HOST_RE` before they reach any external binary.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import socket
import sys
from pathlib import Path
from typing import Awaitable, Callable, Optional


_HOST_RE = re.compile(r"^[a-zA-Z0-9.\-]{1,253}$")
_CMD_TIMEOUT = 10
_INSTALL_ROOT = Path(__file__).resolve().parent.parent.parent
_BHM_LOG = _INSTALL_ROOT / "bhm.log"


async def _run_subprocess(args: list[str], timeout: int = _CMD_TIMEOUT) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode == 0, out.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        return False, "command timeout"
    except FileNotFoundError as e:
        return False, f"binary not found: {e}"


async def _cmd_usb_probe(args: dict) -> dict:
    script = _INSTALL_ROOT / "scripts" / "usb_probe.py"
    if not script.exists():
        return {"ok": False, "error": f"usb_probe.py not found at {script}"}
    ok, out = await _run_subprocess([sys.executable, str(script)])
    return {"ok": ok, "output": out}


async def _cmd_list_interfaces(args: dict) -> dict:
    ok, out = await _run_subprocess(["ip", "-4", "addr", "show"])
    if ok:
        return {"ok": True, "output": out}
    # Fallback to socket.getaddrinfo when `ip` is unavailable (macOS, Termux).
    try:
        addrs = socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET)
        lines = sorted({a[4][0] for a in addrs})
        return {"ok": True, "output": "\n".join(lines)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _cmd_ping(args: dict) -> dict:
    host = str(args.get("host", ""))
    if not _HOST_RE.match(host):
        return {"ok": False, "error": "invalid host"}
    ok, out = await _run_subprocess(["ping", "-c", "3", host])
    return {"ok": ok, "output": out}


async def _cmd_terminal_probe(args: dict) -> dict:
    host = str(args.get("ip", ""))
    try:
        port = int(args.get("port", 3000))
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid port"}
    if not _HOST_RE.match(host):
        return {"ok": False, "error": "invalid ip"}
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=3,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return {"ok": True, "output": f"connected {host}:{port}"}
    except (asyncio.TimeoutError, OSError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _cmd_tail_log(args: dict) -> dict:
    try:
        n = int(args.get("n", 200))
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid n"}
    if not _BHM_LOG.exists():
        return {"ok": False, "error": f"bhm.log not found at {_BHM_LOG}"}
    lines = _BHM_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    return {"ok": True, "output": "\n".join(lines)}


async def _cmd_dump_config(args: dict, config: Optional[dict] = None) -> dict:
    if config is None:
        return {"ok": False, "error": "no config in context"}
    redacted = copy.deepcopy(config)
    server = redacted.get("server", {})
    if "api_key" in server:
        server["api_key"] = "***"
    return {"ok": True, "output": json.dumps(redacted, indent=2, sort_keys=True)}


_DIAGNOSTICS: dict[str, Callable[..., Awaitable[dict]]] = {
    "usb_probe": _cmd_usb_probe,
    "list_interfaces": _cmd_list_interfaces,
    "ping": _cmd_ping,
    "terminal_probe": _cmd_terminal_probe,
    "tail_log": _cmd_tail_log,
    "dump_config": _cmd_dump_config,
}


async def run_diagnostic(cmd: str, args: dict, config: Optional[dict] = None) -> dict:
    fn = _DIAGNOSTICS.get(cmd)
    if fn is None:
        return {"ok": False, "error": f"unknown cmd: {cmd}"}
    if cmd == "dump_config":
        return await fn(args, config=config)
    return await fn(args)


def make_callback(config: dict) -> Callable[[str, str, dict], Awaitable[dict]]:
    """Wraps run_diagnostic into the (cmd_id, cmd, args) -> result-dict shape
    that LogUplinkClient expects. Closes over `config` so dump_config can
    redact it."""
    async def cb(cmd_id: str, cmd: str, args: dict) -> dict:
        result = await run_diagnostic(cmd, args, config=config)
        return {"cmd_id": cmd_id, **result}
    return cb
