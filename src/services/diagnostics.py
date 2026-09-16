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


from src.config import APP_DIR

FROZEN = bool(getattr(sys, "frozen", False))

_HOST_RE = re.compile(r"^[a-zA-Z0-9.\-]{1,253}$")
_CMD_TIMEOUT = 10
# BH-158. Два РІЗНІ корені, і плутати їх не можна (див. коментар у config.py):
#   _BUNDLE_ROOT — спаковані ресурси, тільки на читання (scripts/usb_probe.py).
#                  У замороженій збірці це `_MEIPASS`, куди PyInstaller кладе
#                  datas, — саме туди й треба дивитись за скриптом.
#   APP_DIR      — робочі дані (bhm.log, terminals.json). У мак-застосунку це
#                  ~/.barhandler-manager, а не `_MEIPASS`, тож доти віддалена
#                  діагностика читала лог і термінали за неіснуючим шляхом і
#                  чесно відповідала «не знайдено» на кожен запит підтримки.
_BUNDLE_ROOT = Path(__file__).resolve().parent.parent.parent


def _bhm_log_path() -> Path:
    """Лог менеджера — робочі дані, тож завжди від APP_DIR.

    Функція, а не константа: у вихідному чекауті APP_DIR збігається з коренем
    коду, і константа, обчислена на імпорті, ховає, від ЧОГО вона насправді
    залежить — тест не відрізнив би правильний шлях від помилкового.
    """
    return APP_DIR / "bhm.log"


def _safe_text(value: object) -> str:
    """`str()` від чужого обʼєкта може САМ кинути виняток.

    Знайдено другим колом ревʼю. Форматування `f"{value}"` усередині гілки
    `except` — це рівно та вада, яку ця функція й лікувала: новий виняток
    народжується вже В обробнику, тож сусідній `except Exception` його не
    ловить (гілки не вкладені), і він тікає з діагностики назовні. Досить
    звичайного власного винятку, чий `__str__` звертається до атрибута,
    якого ще немає.
    """
    try:
        return str(value)
    except Exception:  # noqa: BLE001 — саме це ми тут і ловимо
        return f"<{type(value).__name__} без придатного __str__>"


def _run_script_inprocess(script: Path) -> tuple[bool, str]:
    """Виконати самодостатній діагностичний скрипт у власному процесі й
    повернути (успіх, те, що він надрукував).

    Потрібно лише для замороженої збірки, де окремого інтерпретатора немає.
    Скрипт кличе `sys.exit(1)` на своїх помилках — це нормальне завершення, а
    не збій діагностики, тож SystemExit ловимо й читаємо його код. Виконуємо
    з `__name__ == "__main__"`, інакше блок у кінці файла не запуститься.

    Вивід забираємо ПІДМІНОЮ `print` у власному просторі імен скрипта, а не
    через `contextlib.redirect_stdout`: той підміняє `sys.stdout` на весь
    процес, і поки USB-перевірка йде (а це секунди), у наш буфер падали б
    рядки чужих запитів, які в цей момент теж щось друкують. Пошук імені в
    модулі спершу дивиться у глобальні змінні й лише потім у builtins, тож
    підміни в словнику достатньо — і вона не виходить за межі скрипта.
    """
    import io

    buf = io.StringIO()
    code = 0
    try:
        source = script.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"can't read {script}: {exc}"

    def _print(*args, sep: str = " ", end: str = "\n", **_kwargs) -> None:
        buf.write(sep.join(str(a) for a in args) + end)

    namespace = {"__name__": "__main__", "__file__": str(script), "print": _print}
    try:
        exec(compile(source, str(script), "exec"), namespace)
    except SystemExit as exc:
        # `sys.exit("текст")` — звичайний пітонівський ідіом, і тоді `code` це
        # рядок. Знайдено ревʼю: `int(...)` на ньому кидав ValueError ПРЯМО В
        # except-гілці, тобто повз `except Exception` нижче — і виняток тікав
        # аж у сокет-колбек, валячи те, що ця функція мала б ловити.
        # Рядок у `code` за угодою означає помилку — і сам текст теж треба
        # показати, бо саме він пояснює, що не так.
        if isinstance(exc.code, int):
            code = exc.code
        elif exc.code is None:
            code = 0
        else:
            buf.write(_safe_text(exc.code) + "\n")
            code = 1
    except Exception as exc:  # noqa: BLE001 — діагностика не має валити менеджер
        return False, buf.getvalue() + f"\n{type(exc).__name__}: {_safe_text(exc)}"
    return code == 0, buf.getvalue()


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


def _usb_probe_script() -> Optional[Path]:
    """Шлях до usb_probe.py — спакованого ресурсу. None, якщо його немає."""
    script = _BUNDLE_ROOT / "scripts" / "usb_probe.py"
    return script if script.exists() else None


async def _cmd_usb_probe(args: dict) -> dict:
    script = _usb_probe_script()
    if script is None:
        return {
            "ok": False,
            "error": f"usb_probe.py not found at {_BUNDLE_ROOT / 'scripts' / 'usb_probe.py'}",
        }
    if FROZEN:
        # У замороженій збірці `sys.executable` — це САМ менеджер, а не
        # інтерпретатор: запуск підпроцесом підняв би другий менеджер, який
        # воює за порт 9999. Тому виконуємо скрипт у себе в процесі й
        # перехоплюємо його вивід.
        #
        # Стеля часу така сама, як у підпроцесного шляху, щоб зависла на
        # libusb перевірка не тримала HTTP-запит вічно. Чесно: потік ми на
        # цьому не вбиваємо (нитку в Python не скасувати) — він дорахує сам
        # і результат нікому не віддасть; це все одно краще, ніж запит,
        # який не повертається ніколи.
        try:
            ok, out = await asyncio.wait_for(
                asyncio.to_thread(_run_script_inprocess, script),
                timeout=_CMD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return {"ok": False, "error": "command timeout"}
        return {"ok": ok, "output": out}
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
    log_path = _bhm_log_path()
    if not log_path.exists():
        return {"ok": False, "error": f"bhm.log not found at {log_path}"}
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    return {"ok": True, "output": "\n".join(lines)}


def _terminals_path(config: Optional[dict]) -> Path:
    """Той САМИЙ файл, який читає TerminalRegistry: шлях із конфіга, а якщо
    він відносний — від APP_DIR, а не від поточної теки процесу."""
    raw = ((config or {}).get("server") or {}).get("terminal_registry_path") or "terminals.json"
    path = Path(raw)
    return path if path.is_absolute() else APP_DIR / path


async def _cmd_list_terminals(args: dict, config: Optional[dict] = None) -> dict:
    """Read terminals.json and return registered terminals."""
    terminals_path = _terminals_path(config)
    if not terminals_path.exists():
        return {"ok": True, "output": "[]", "note": f"terminals.json not found at {terminals_path}"}
    try:
        content = terminals_path.read_text(encoding="utf-8", errors="replace")
        return {"ok": True, "output": content}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _cmd_dump_config(args: dict, config: Optional[dict] = None) -> dict:
    if config is None:
        return {"ok": False, "error": "no config in context"}
    redacted = copy.deepcopy(config)
    server = redacted.get("server", {})
    if "api_key" in server:
        server["api_key"] = "***"
    return {"ok": True, "output": json.dumps(redacted, indent=2, sort_keys=True)}


async def _cmd_list_subnets(args: dict) -> dict:
    """Show every /24 the multi-interface enumerator detected.
    Useful for confirming the scan will actually look where the
    terminal lives — e.g. CGNAT carrier subnet on a phone, or hotspot
    AP subnet on a tablet."""
    try:
        from src.devices.scan import _local_subnets
        nets = [str(n) for n in _local_subnets()]
        return {"ok": True, "output": "\n".join(nets) if nets else "(no subnets)"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _cmd_terminal_probe_protocol(args: dict) -> dict:
    """Probe a specific host:port with SSI (Mono), PB (PrivatBank), or both protocols.
    Args: ip, port (default 3000), protocol: 'ssi' | 'pb' | 'both' (default 'both')
    """
    host = str(args.get("ip", ""))
    if not _HOST_RE.match(host):
        return {"ok": False, "error": "invalid ip"}
    try:
        port = int(args.get("port", 3000))
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid port"}
    protocol = str(args.get("protocol", "both")).lower()
    if protocol not in ("ssi", "pb", "both"):
        return {"ok": False, "error": "protocol must be ssi, pb, or both"}

    from src.services.terminals.ssi import SSITerminalAdapter
    from src.services.terminals.privatbank import PrivatBankTerminalAdapter

    results = {}
    if protocol in ("ssi", "both"):
        try:
            descriptor = await SSITerminalAdapter.probe(host, port)
            results["ssi"] = descriptor.model_dump(mode="json") if descriptor else None
        except Exception as e:
            results["ssi"] = {"error": f"{type(e).__name__}: {e}"}
    if protocol in ("pb", "both"):
        try:
            descriptor = await PrivatBankTerminalAdapter.probe(host, port)
            results["pb"] = descriptor.model_dump(mode="json") if descriptor else None
        except Exception as e:
            results["pb"] = {"error": f"{type(e).__name__}: {e}"}

    found = {k: v for k, v in results.items() if v is not None and "error" not in v}
    return {
        "ok": True,
        "found": bool(found),
        "output": json.dumps(results, indent=2, ensure_ascii=False),
    }


async def _cmd_terminal_scan_subnet(args: dict) -> dict:
    """Scan a specific subnet for terminals using both SSI and PB protocols.
    Args: subnet — CIDR notation, e.g. '10.245.122.0/24'
    """
    import ipaddress
    subnet_str = str(args.get("subnet", ""))
    try:
        subnet = ipaddress.ip_network(subnet_str, strict=False)
    except ValueError:
        return {"ok": False, "error": f"invalid subnet: {subnet_str!r}"}
    if subnet.prefixlen < 16:
        return {"ok": False, "error": "subnet too large (minimum /16)"}

    try:
        from src.devices.scan import SSI_TCP_PORT, PB_TCP_PORT, _probe_tcp, discover_network_terminals
        import concurrent.futures

        hosts = [str(h) for h in subnet.hosts()]
        open_pairs: list[tuple[str, int]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
            future_map = {}
            for host in hosts:
                future_map[pool.submit(_probe_tcp, host, SSI_TCP_PORT, 0.3)] = (host, SSI_TCP_PORT)
                future_map[pool.submit(_probe_tcp, host, PB_TCP_PORT, 0.3)] = (host, PB_TCP_PORT)
            for future in concurrent.futures.as_completed(future_map):
                host, port = future_map[future]
                try:
                    if future.result():
                        open_pairs.append((host, port))
                except Exception:
                    pass

        if not open_pairs:
            return {"ok": True, "found": False, "output": "[]"}

        from src.services.terminals.ssi import SSITerminalAdapter
        from src.services.terminals.privatbank import PrivatBankTerminalAdapter

        async def _probe_all() -> list:
            out, seen = [], set()
            for host, port in open_pairs:
                for adapter, label in ((SSITerminalAdapter, "ssi"), (PrivatBankTerminalAdapter, "pb")):
                    key = f"{host}:{port}:{label}"
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        d = await adapter.probe(host, port)
                    except Exception:
                        d = None
                    if d is not None:
                        out.append(d)
            return out

        descriptors = await _probe_all()
        out = [d.model_dump(mode="json") for d in descriptors]
        return {"ok": True, "found": bool(out), "output": json.dumps(out, indent=2, ensure_ascii=False)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _cmd_terminal_discover(args: dict) -> dict:
    """Run /terminal/discover synchronously and return the JSON-able list of
    descriptors. Equivalent to a dashboard 'Сканувати термінали' click but
    callable remotely without X-Api-Key."""
    try:
        from src.devices.scan import discover_network_terminals
        descriptors = await asyncio.to_thread(discover_network_terminals)
        out = [d.model_dump(mode="json") for d in descriptors]
        return {"ok": True, "output": json.dumps(out, indent=2, ensure_ascii=False)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


_DIAGNOSTICS: dict[str, Callable[..., Awaitable[dict]]] = {
    "usb_probe": _cmd_usb_probe,
    "list_interfaces": _cmd_list_interfaces,
    "list_subnets": _cmd_list_subnets,
    "ping": _cmd_ping,
    "terminal_probe": _cmd_terminal_probe,
    "terminal_probe_protocol": _cmd_terminal_probe_protocol,
    "terminal_scan_subnet": _cmd_terminal_scan_subnet,
    "terminal_discover": _cmd_terminal_discover,
    "tail_log": _cmd_tail_log,
    "list_terminals": _cmd_list_terminals,
    "dump_config": _cmd_dump_config,
}

# Команди, яким потрібен конфіг менеджера — його передає make_callback().
_NEEDS_CONFIG = {"dump_config", "list_terminals"}


async def run_diagnostic(cmd: str, args: dict, config: Optional[dict] = None) -> dict:
    fn = _DIAGNOSTICS.get(cmd)
    if fn is None:
        return {"ok": False, "error": f"unknown cmd: {cmd}"}
    if cmd in _NEEDS_CONFIG:
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
