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
import locale
import re
import socket
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional, Any


from src.config import APP_DIR

FROZEN = bool(getattr(sys, "frozen", False))

_HOST_RE = re.compile(r"^[a-zA-Z0-9.\-]{1,253}$")
# The one token of `ping.exe` output that says the HOST answered, not a router.
_TTL_RE = re.compile(r"\bTTL\s*=", re.IGNORECASE)
_CMD_TIMEOUT = 10
# PET-1159 — the server waits this long for an answer to a diagnostic
# (`awaitDiagnostic` in managers.service.ts) and then says «diagnostic
# timeout», while the scan goes on running on the customer's machine with
# nobody left to hand the result to. A /16 is 65 534 hosts × 2 ports at 0.3 s
# over 64 threads — about ten minutes, so every subnet wider than this was
# guaranteed to end that way. A /22 is 1 022 hosts, about ten seconds.
_SCAN_WAIT_S = 30
_SCAN_MIN_PREFIX = 22
# What the scan itself may spend: the answer has to travel back inside the
# server's window, and a scan that overruns it costs the whole result.
_SCAN_BUDGET_S = _SCAN_WAIT_S - 5
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


_IS_WINDOWS = sys.platform.startswith("win")
# By number: `subprocess.CREATE_NO_WINDOW` does not exist on posix, so even
# naming it there breaks (same reasoning as in `routes/system.py`).
_WIN_CREATE_NO_WINDOW = 0x08000000


def _console_cp() -> int:
    """The code page the console writes in; 0 when there is no console."""
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleOutputCP())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — not Windows, or no console attached
        return 0


def _oem_cp() -> int:
    """The system's OEM page — what a console-less parent's children write in.

    Found by the review: the shipped manager is built with `console=False`
    (bhm.spec), so it has no console at all and `GetConsoleOutputCP()` answers
    0. A `ping.exe` started from it gets a FRESH console, and that one writes in
    the OEM page (cp866 / cp1125 / cp437) — not in the ANSI page that
    `locale.getpreferredencoding()` reports. Reading OEM bytes as cp1251 does
    not fail either: it returns plausible-looking rubbish, which is the very
    defect of this ticket wearing different junk.
    """
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetOEMCP())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return 0


def _console_encodings() -> list[str]:
    """PET-1159 — what the output of a command may be written in.

    A Windows console answers in the system's code page (cp866 for Cyrillic),
    never in utf-8. Decoding it as utf-8 with `errors="replace"` does not fail
    — it quietly turns the answer into a row of replacement characters, and
    that reads as a command behaving strangely rather than as a mistake of
    ours about the encoding.
    """
    if not _IS_WINDOWS:
        return ["utf-8"]
    found: list[str] = []
    for cp in (_console_cp(), _oem_cp()):
        if cp:
            # 65001 is utf-8 and Python knows it under that name too.
            found.append("utf-8" if cp == 65001 else f"cp{cp}")
    # The ANSI page LAST and never before the OEM one: it decodes OEM bytes
    # without raising and so hides the mistake instead of showing it.
    try:
        found.append(locale.getpreferredencoding(False))
    except Exception:  # noqa: BLE001
        pass
    found.append("utf-8")
    # In order, without repeats.
    return [e for e in dict.fromkeys(found) if e]


def _decode_output(raw: bytes) -> str:
    for enc in _console_encodings():
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # Last resort: never let the diagnostics fail over an encoding.
    return raw.decode("utf-8", errors="replace")


def _ping_args(host: str, count: int = 3) -> list[str]:
    """PET-1159 — the count of packets, named the way THIS system names it.

    Windows `ping.exe` does not know `-c`: there it means something else
    entirely and the count is `-n`. The command came back with its usage text
    and a non-zero code, so the diagnostics reported "host unreachable" for
    every address, a live one included. The manager runs on the customer's
    machine under Windows, so that was the only platform where it mattered —
    on a developer's Linux box it looked sound.

    `-w` on Windows is the wait for ONE reply in milliseconds: without it the
    default is 4 s per packet, and three dead packets outlast the 10 s cap on
    the command, turning a useful answer into "command timeout". The Linux and
    macOS flags of that name mean other things, so they are left alone there.
    """
    if _IS_WINDOWS:
        return ["ping", "-n", str(count), "-w", "1000", host]
    return ["ping", "-c", str(count), host]


async def _run_subprocess(args: list[str], timeout: int = _CMD_TIMEOUT) -> tuple[bool, str]:
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # The manager is built without a console (bhm.spec), so every child
            # of ours would otherwise flash a console window in front of the
            # cashier. The same flag, by number, as `routes/system.py` uses —
            # the name does not exist in `subprocess` on posix.
            **({"creationflags": _WIN_CREATE_NO_WINDOW} if _IS_WINDOWS else {}),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode == 0, _decode_output(out)
    except asyncio.TimeoutError:
        # Found by the review: without this the child outlives the command it
        # was started for — a `ping.exe` that hangs stays for the life of the
        # manager, and nothing ever reads its output.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001 — it is already gone
                pass
        return False, "command timeout"
    except FileNotFoundError as e:
        return False, f"binary not found: {e}"
    except (NotImplementedError, OSError) as e:
        # A loop that cannot spawn children (Selector on Windows), or no rights
        # to. Diagnostics must say so, not raise out of the dispatcher.
        return False, f"{type(e).__name__}: {e}"


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
    # PET-1159, the same class as the ping above: `ip` is a Linux binary and
    # simply is not there on the customer's Windows machine, so this fell
    # through to a bare list of addresses — without a netmask or an interface
    # name, which is most of what one asks this command for.
    probe = ["ipconfig", "/all"] if _IS_WINDOWS else ["ip", "-4", "addr", "show"]
    ok, out = await _run_subprocess(probe)
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
    ok, out = await _run_subprocess(_ping_args(host))
    if ok and _IS_WINDOWS:
        # Found by the review: `ping.exe` exits 0 for a reply of ANY kind,
        # including the router's own "Destination host unreachable" and "TTL
        # expired in transit" — which is the ordinary answer for a dead host on
        # the same LAN. Taken from the exit code alone, this fix would have
        # swapped "a live host reported unreachable" for "a dead host reported
        # ok". Only a reply from the host itself carries a TTL, and that token
        # is not translated in any language pack we ship to.
        ok = bool(_TTL_RE.search(out))
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


_TAIL_LOG_ARGS = {"n"}


async def _cmd_tail_log(args: dict) -> dict:
    # BH-159 — невідомий аргумент це ПОМИЛКА, а не привід мовчки взяти дефолт.
    # Підтримка передала `lines` замість `n`, отримала дефолтні 200 рядків без
    # жодної скарги — і зробила висновок про обсяг лога, який був неправдою.
    unknown = sorted(set(args or {}) - _TAIL_LOG_ARGS)
    if unknown:
        return {
            "ok": False,
            "error": f"unknown arg(s): {', '.join(unknown)} — tail_log takes only 'n'",
        }
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
    if subnet.prefixlen < _SCAN_MIN_PREFIX:
        return {
            "ok": False,
            "error": (
                f"subnet too large: /{subnet.prefixlen} is {subnet.num_addresses} "
                f"addresses and would take minutes, while the server stops "
                f"waiting for a diagnostic after {_SCAN_WAIT_S}s — "
                f"use /{_SCAN_MIN_PREFIX} or smaller"
            ),
        }

    started = time.monotonic()

    def _left() -> float:
        return _SCAN_BUDGET_S - (time.monotonic() - started)

    try:
        from src.devices.scan import SSI_TCP_PORT, PB_TCP_PORT, _probe_tcp

        hosts = [str(h) for h in subnet.hosts()]

        def _sweep() -> list[tuple[str, int]]:
            import concurrent.futures

            pairs: list[tuple[str, int]] = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
                future_map = {}
                for host in hosts:
                    for port in (SSI_TCP_PORT, PB_TCP_PORT):
                        future_map[pool.submit(_probe_tcp, host, port, 0.3)] = (host, port)
                for future in concurrent.futures.as_completed(future_map):
                    host, port = future_map[future]
                    try:
                        if future.result():
                            pairs.append((host, port))
                    except Exception:  # noqa: BLE001 — a closed port is the normal case
                        pass
            return pairs

        # Found by the review: the sweep is blocking, and this callback runs on
        # the SERVER's own loop — so a scan held the till's whole HTTP API, and
        # the socket.io heartbeat with it, for as long as it ran. In a thread,
        # the way `_cmd_terminal_discover` already does it.
        open_pairs = await asyncio.to_thread(_sweep)
        if not open_pairs:
            return {"ok": True, "found": False, "output": "[]"}

        from src.services.terminals.ssi import SSITerminalAdapter
        from src.services.terminals.privatbank import PrivatBankTerminalAdapter

        # Found by the review: the TCP sweep fits the budget, but the PROBES did
        # not — they ran one after another, 2 s each on anything that listens on
        # 3000 or 2000 without being a terminal (a NAS, a dashboard, a dev
        # server). Eight such hosts turned a 10 s scan into 40 s, that is back
        # into «diagnostic timeout». Now they go together, and what does not fit
        # the budget is said out loud instead of costing the whole answer.
        sem = asyncio.Semaphore(8)

        async def _probe(host: str, port: int, adapter) -> object:
            async with sem:
                try:
                    return await adapter.probe(host, port)
                except Exception:  # noqa: BLE001
                    return None

        tasks = [
            asyncio.create_task(_probe(host, port, adapter))
            for host, port in open_pairs
            for adapter in (SSITerminalAdapter, PrivatBankTerminalAdapter)
        ]
        done, pending = await asyncio.wait(tasks, timeout=max(1.0, _left()))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        descriptors = [
            task.result()
            for task in done
            if not task.cancelled() and task.exception() is None and task.result() is not None
        ]
        out = [d.model_dump(mode="json") for d in descriptors]
        result = {
            "ok": True,
            "found": bool(out),
            "output": json.dumps(out, indent=2, ensure_ascii=False),
        }
        if pending:
            result["partial"] = True
            result["note"] = (
                f"stopped after {_SCAN_BUDGET_S}s so that the answer still "
                f"reaches the server (it waits {_SCAN_WAIT_S}s): "
                f"{len(pending)} of {len(tasks)} probes were not finished. "
                f"Narrow the subnet, or probe a single address with "
                f"terminal_probe_protocol."
            )
        return result
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


def make_callback(
    config: dict,
    app_state: Optional[Any] = None,
) -> Callable[[str, str, dict], Awaitable[dict]]:
    """Wraps run_diagnostic into the (cmd_id, cmd, args) -> result-dict shape
    that LogUplinkClient expects. Closes over `config` so dump_config can
    redact it, and over the app state so PET-928's `uplink_off` can reach the
    running client."""
    async def cb(cmd_id: str, cmd: str, args: dict) -> dict:
        if cmd == "uplink_off":
            return {"cmd_id": cmd_id, **await _stop_uplink(app_state, config)}
        result = await run_diagnostic(cmd, args, config=config)
        return {"cmd_id": cmd_id, **result}
    return cb


async def _stop_uplink(app_state: Optional[Any], config: dict) -> dict:
    """PET-928 — switch remote diagnostics off, asked from the server.

    Whoever asked for the door to be opened is rarely the one sitting at the
    machine, so closing it must not depend on them.

    The shutdown is handed back as `_after_reply` rather than done here: it
    takes away the very socket this command's answer travels on, so it has to
    wait until the answer has actually been sent. `_on_diagnostic` runs it
    immediately after awaiting its own emit — a real ordering, not a guess at
    how long a send takes.
    """
    if app_state is None:
        return {"ok": False, "error": "no app state on this manager"}
    from src.services.uplink_life import shut_down

    async def after() -> None:
        await shut_down(app_state, config, "switched off from the server")

    return {
        "ok": True,
        "output": "remote diagnostics stopping",
        "_after_reply": after,
    }
