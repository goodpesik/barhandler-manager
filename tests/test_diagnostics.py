import asyncio

import pytest

from src.services.diagnostics import run_diagnostic


@pytest.mark.asyncio
async def test_unknown_cmd():
    r = await run_diagnostic("nope", {})
    assert r["ok"] is False
    assert "unknown" in r["error"].lower()


@pytest.mark.asyncio
async def test_dump_config_redacts():
    cfg = {"server": {"api_key": "SECRET", "port": 9999}, "uplink": {"enabled": True}}
    r = await run_diagnostic("dump_config", {}, config=cfg)
    assert r["ok"] is True
    assert "SECRET" not in r["output"]
    assert "***" in r["output"]


@pytest.mark.asyncio
async def test_dump_config_without_context():
    r = await run_diagnostic("dump_config", {})
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_ping_validates_host():
    r = await run_diagnostic("ping", {"host": "evil; rm -rf /"})
    assert r["ok"] is False
    assert "invalid host" in r["error"].lower()


@pytest.mark.asyncio
async def test_list_interfaces_returns_addresses():
    r = await run_diagnostic("list_interfaces", {})
    assert r["ok"] is True
    assert isinstance(r["output"], str)
    assert len(r["output"]) > 0


@pytest.mark.asyncio
async def test_terminal_probe_invalid_ip():
    r = await run_diagnostic("terminal_probe", {"ip": "; evil"})
    assert r["ok"] is False
    assert "invalid ip" in r["error"].lower()


@pytest.mark.asyncio
async def test_tail_log_invalid_n():
    r = await run_diagnostic("tail_log", {"n": "abc"})
    assert r["ok"] is False


# --- PET-928: switching remote diagnostics off from the server -------------


@pytest.mark.asyncio
async def test_uplink_off_is_not_an_unknown_command():
    """It travels the same socket as every other diagnostic command.

    The one it is NOT is a plain entry in the command table: closing the door
    needs the running client, which the table's functions cannot reach.
    """
    from types import SimpleNamespace

    from src.services.diagnostics import make_callback

    cfg = {"uplink": {"enabled": True, "enabled_at": "2026-09-25T12:00:00+00:00"}}
    state = SimpleNamespace(uplink=None)
    cb = make_callback(cfg, state)
    r = await cb("cmd-1", "uplink_off", {})
    assert r["ok"] is True
    assert r["cmd_id"] == "cmd-1"


@pytest.mark.asyncio
async def test_uplink_off_hands_the_shutdown_back_instead_of_doing_it(monkeypatch):
    """The command answers; the socket goes only once that answer is sent.

    Stopping it here would take away the very connection the reply travels on.
    Sleeping «long enough» first was a guess at how long a send takes; the
    shutdown is handed back as `_after_reply` so the caller can run it right
    after awaiting the emit.
    """
    from types import SimpleNamespace

    from src.services.diagnostics import make_callback

    stopped = []

    class Client:
        async def stop(self):
            stopped.append("stopped")

        def detach_handler_from_root(self):
            pass

    monkeypatch.setattr("src.routes.system.persist_uplink_state", lambda u: None)
    cfg = {"uplink": {"enabled": True, "enabled_at": "2026-09-25T12:00:00+00:00"}}
    state = SimpleNamespace(uplink=Client())
    cb = make_callback(cfg, state)

    r = await cb("cmd-2", "uplink_off", {})
    assert r["ok"] is True
    after = r["_after_reply"]
    # Nothing has been taken away yet.
    assert stopped == []
    await after()
    assert stopped == ["stopped"]
    assert cfg["uplink"]["enabled"] is False


@pytest.mark.asyncio
async def test_the_reply_is_emitted_before_the_shutdown_runs(monkeypatch):
    """Proven through the real dispatcher, in order, with no timing guess."""
    from types import SimpleNamespace

    from src.services.log_uplink import LogUplinkClient

    order = []

    client = LogUplinkClient({"url": "https://x", "tenant": "t", "reconnect_delay": 1})

    async def fake_emit(event, payload):
        order.append(("emit", event, payload.get("ok")))

    monkeypatch.setattr(client, "_safe_emit", fake_emit)

    async def after():
        order.append(("shutdown",))

    async def cb(cmd_id, cmd, args):
        return {"cmd_id": cmd_id, "ok": True, "_after_reply": after}

    client.set_diagnostics_callback(cb)
    await client._on_diagnostic({"cmd_id": "c1", "cmd": "uplink_off", "args": {}})

    assert order == [("emit", "diagnostic_result", True), ("shutdown",)]


@pytest.mark.asyncio
async def test_a_failing_after_step_does_not_break_the_dispatcher(monkeypatch):
    from src.services.log_uplink import LogUplinkClient

    emitted = []
    client = LogUplinkClient({"url": "https://x", "tenant": "t", "reconnect_delay": 1})

    async def fake_emit(event, payload):
        emitted.append(payload)

    monkeypatch.setattr(client, "_safe_emit", fake_emit)

    async def after():
        raise RuntimeError("socket already gone")

    async def cb(cmd_id, cmd, args):
        return {"cmd_id": cmd_id, "ok": True, "_after_reply": after}

    client.set_diagnostics_callback(cb)
    # The answer still went out; the failure is logged, not raised.
    await client._on_diagnostic({"cmd_id": "c1", "cmd": "uplink_off", "args": {}})
    assert emitted and emitted[0]["ok"] is True


@pytest.mark.asyncio
async def test_the_marker_never_travels_to_the_server(monkeypatch):
    # `_after_reply` is a local coroutine, not something to serialise.
    from src.services.log_uplink import LogUplinkClient

    emitted = []
    client = LogUplinkClient({"url": "https://x", "tenant": "t", "reconnect_delay": 1})

    async def fake_emit(event, payload):
        emitted.append(payload)

    monkeypatch.setattr(client, "_safe_emit", fake_emit)

    async def after():
        return None

    async def cb(cmd_id, cmd, args):
        return {"cmd_id": cmd_id, "ok": True, "_after_reply": after}

    client.set_diagnostics_callback(cb)
    await client._on_diagnostic({"cmd_id": "c1", "cmd": "uplink_off", "args": {}})
    assert "_after_reply" not in emitted[0]


@pytest.mark.asyncio
async def test_uplink_off_says_so_when_it_cannot_reach_the_manager():
    from src.services.diagnostics import make_callback

    cb = make_callback({"uplink": {"enabled": True}}, None)
    r = await cb("cmd-3", "uplink_off", {})
    assert r["ok"] is False
    assert "state" in r["error"]


@pytest.mark.asyncio
async def test_an_ordinary_command_still_works_through_the_same_callback():
    # The mirror: routing `uplink_off` specially must not swallow the rest.
    from src.services.diagnostics import make_callback

    cfg = {"server": {"api_key": "SECRET"}, "uplink": {"enabled": True}}
    cb = make_callback(cfg, None)
    r = await cb("cmd-4", "dump_config", {})
    assert r["ok"] is True
    assert "SECRET" not in r["output"]


# ---------------------------------------------------------------------------
# PET-1159 — the commands themselves worked wrongly on the one platform that
# matters: the manager is installed on the CUSTOMER'S machine, under Windows.
# On a developer's Linux box all four of these looked sound.
# ---------------------------------------------------------------------------


# What `ping.exe` really prints: a reply from the HOST carries a TTL...
_WIN_PING_ALIVE = (
    "Обмін пакетами з 192.168.0.1 за 32 байт даних:\r\n"
    "Відповідь від 192.168.0.1: байт=32 час<1мс TTL=64\r\n"
)
# ...while the router answering FOR a dead host does not, and `ping.exe`
# still exits 0 on it.
_WIN_PING_UNREACHABLE = (
    "Обмін пакетами з 192.168.0.77 за 32 байт даних:\r\n"
    "Відповідь від 192.168.0.1: Вузол призначення недоступний.\r\n"
)


def _capture_subprocess(monkeypatch, output="ok", ok=True):
    """Capture the argv a command would run instead of running it."""
    from src.services import diagnostics

    seen: list[list[str]] = []

    async def fake_run(args, timeout=10):
        seen.append(list(args))
        return ok, output

    monkeypatch.setattr(diagnostics, "_run_subprocess", fake_run)
    return seen


@pytest.mark.asyncio
async def test_ping_counts_packets_the_way_windows_names_it(monkeypatch):
    """`-c` is not the count on Windows — `ping.exe` answers with its usage
    text and a non-zero code, so the diagnostics said "unreachable" about
    every address, a live one included."""
    from src.services import diagnostics

    seen = _capture_subprocess(monkeypatch, output=_WIN_PING_ALIVE)
    monkeypatch.setattr(diagnostics, "_IS_WINDOWS", True)

    r = await run_diagnostic("ping", {"host": "192.168.0.1"})

    assert r["ok"] is True
    assert seen == [["ping", "-n", "3", "-w", "1000", "192.168.0.1"]]
    assert "-c" not in seen[0]


@pytest.mark.asyncio
async def test_a_router_answering_for_a_dead_host_is_not_a_live_host(monkeypatch):
    """`ping.exe` exits 0 for a reply of ANY kind.

    "Вузол призначення недоступний" comes from the GATEWAY, not from the
    address we asked about, and it is the ordinary answer for a dead host on
    the same LAN. Taken from the exit code alone, this fix would have swapped
    one lie for its opposite. Found by the review.
    """
    from src.services import diagnostics

    _capture_subprocess(monkeypatch, output=_WIN_PING_UNREACHABLE, ok=True)
    monkeypatch.setattr(diagnostics, "_IS_WINDOWS", True)

    r = await run_diagnostic("ping", {"host": "192.168.0.77"})

    assert r["ok"] is False, "a router's refusal is not the host answering"
    assert "недоступний" in r["output"], "and the person still sees what came back"


@pytest.mark.asyncio
async def test_ping_keeps_the_posix_flag_everywhere_else(monkeypatch):
    from src.services import diagnostics

    seen = _capture_subprocess(monkeypatch)
    monkeypatch.setattr(diagnostics, "_IS_WINDOWS", False)

    await run_diagnostic("ping", {"host": "192.168.0.1"})

    assert seen == [["ping", "-c", "3", "192.168.0.1"]]


@pytest.mark.asyncio
async def test_list_interfaces_asks_windows_in_its_own_language(monkeypatch):
    """`ip` is a Linux binary: on the customer's machine it is simply not
    there, and the command fell through to a list of bare addresses without a
    netmask or an interface name — most of what it is asked for."""
    from src.services import diagnostics

    seen = _capture_subprocess(monkeypatch)
    monkeypatch.setattr(diagnostics, "_IS_WINDOWS", True)

    await run_diagnostic("list_interfaces", {})

    assert seen == [["ipconfig", "/all"]]


def test_console_output_is_read_in_the_consoles_own_code_page(monkeypatch):
    """A Windows console answers in the system's code page, never in utf-8.

    `errors="replace"` did not fail on that — it quietly turned the answer
    into a row of replacement characters, which reads as a command behaving
    strangely and not as a mistake of ours.
    """
    from src.services import diagnostics

    said = "Відповідь від 192.168.0.1: байт=32 час<1мс TTL=64"
    raw = said.encode("cp1125")  # the Ukrainian DOS page a console uses
    monkeypatch.setattr(diagnostics, "_IS_WINDOWS", True)
    monkeypatch.setattr(diagnostics, "_console_cp", lambda: 1125)

    assert diagnostics._decode_output(raw) == said
    # The control: read as utf-8 the same bytes carry no answer at all.
    assert "\ufffd" in raw.decode("utf-8", errors="replace")


def test_with_no_console_the_oem_page_is_used_and_not_the_ansi_one(monkeypatch):
    """The shipped manager is built WITHOUT a console (bhm.spec), so this is
    the ordinary case, not the exotic one.

    `GetConsoleOutputCP()` then answers 0, and a child gets a fresh console in
    the OEM page. Reading those bytes as the ANSI page does not raise — it
    returns plausible-looking rubbish, which is this ticket's defect wearing
    different junk. Found by the review.
    """
    from src.services import diagnostics

    said = "Відповідь від 192.168.0.1: байт=32 час<1мс TTL=64"
    raw = said.encode("cp1125")
    monkeypatch.setattr(diagnostics, "_IS_WINDOWS", True)
    monkeypatch.setattr(diagnostics, "_console_cp", lambda: 0)
    monkeypatch.setattr(diagnostics, "_oem_cp", lambda: 1125)
    # The ANSI page of such a machine — what the old order would have picked.
    monkeypatch.setattr(diagnostics.locale, "getpreferredencoding", lambda *a: "cp1251")

    assert diagnostics._decode_output(raw) == said
    assert raw.decode("cp1251") != said, "the control: the ANSI page reads it wrongly"


def test_output_is_still_readable_when_no_page_can_be_asked(monkeypatch):
    from src.services import diagnostics

    monkeypatch.setattr(diagnostics, "_IS_WINDOWS", True)
    monkeypatch.setattr(diagnostics, "_console_cp", lambda: 0)
    monkeypatch.setattr(diagnostics, "_oem_cp", lambda: 0)
    # Nothing raises, and plain ASCII survives whatever page is picked.
    assert "Reply from 192.168.0.1" in diagnostics._decode_output(
        b"Reply from 192.168.0.1",
    )


@pytest.mark.asyncio
async def test_a_subnet_wider_than_the_server_waits_for_is_refused_by_name():
    """A /16 took about ten minutes while the server gives up after 30 s: the
    scan went on running on the customer's machine with nobody left to hand
    the answer to, and all the person saw was «diagnostic timeout»."""
    r = await run_diagnostic("terminal_scan_subnet", {"subnet": "10.0.0.0/16"})

    assert r["ok"] is False
    assert "30" in r["error"], f"the reason has to name the budget: {r['error']}"
    assert "/22" in r["error"], "and what to use instead"

    # Not only the extreme case: a /20 is 4 094 hosts, which does not fit either.
    r20 = await run_diagnostic("terminal_scan_subnet", {"subnet": "10.0.0.0/20"})
    assert r20["ok"] is False


@pytest.mark.asyncio
async def test_a_subnet_that_fits_is_scanned(monkeypatch):
    """The ceiling must still let through what the budget allows."""
    import src.devices.scan as scan

    monkeypatch.setattr(scan, "_probe_tcp", lambda host, port, timeout=0.3: False)

    r = await run_diagnostic("terminal_scan_subnet", {"subnet": "10.0.0.0/22"})

    assert r["ok"] is True
    assert r["found"] is False


@pytest.mark.asyncio
async def test_a_scan_that_outgrows_the_budget_answers_with_what_it_has(monkeypatch):
    """The TCP sweep fits the budget; the PROBES did not.

    Found by the review: each open host:port was probed with two adapters one
    after another, about 2 s each, so eight hosts listening on 3000 without
    being terminals turned a 10 s scan into 40 s — back into «diagnostic
    timeout», with the scan still running and nobody to answer. Now the answer
    comes back inside the window and says it is partial.
    """
    from src.services import diagnostics
    import src.devices.scan as scan
    from src.services.terminals.ssi import SSITerminalAdapter
    from src.services.terminals.privatbank import PrivatBankTerminalAdapter

    # Three hosts answer the TCP connect, and every protocol probe hangs.
    monkeypatch.setattr(
        scan, "_probe_tcp",
        lambda host, port, timeout=0.3: host.endswith((".1", ".2", ".3")),
    )

    async def never_answers(host, port, *a, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(SSITerminalAdapter, "probe", staticmethod(never_answers))
    monkeypatch.setattr(PrivatBankTerminalAdapter, "probe", staticmethod(never_answers))
    monkeypatch.setattr(diagnostics, "_SCAN_BUDGET_S", 1)

    r = await asyncio.wait_for(
        run_diagnostic("terminal_scan_subnet", {"subnet": "10.0.0.0/29"}),
        timeout=20,
    )

    assert r["ok"] is True, r
    assert r.get("partial") is True, f"the answer has to say it is partial: {r}"
    assert "30" in r["note"], "and name the window it had to fit"
    assert r["found"] is False
