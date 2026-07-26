# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build of barhandler-manager as ONE headless Windows exe.

Built on a `windows-latest` GitHub runner — see:
  - .github/workflows/build-exe-dev.yml  (manual test channel → `nightly` prerelease)
  - .github/workflows/publish.yml        (release channel → attached to the vX.Y.Z release)

`console=False` → no console window (behaves like the current `pythonw.exe`
headless run). No COLLECT step → single-file exe.

NOTE (expected CI iteration): the tricky bits are (a) libusb for pyusb on
Windows — we pip-install `libusb-package` in the workflow and `collect_all`
its DLL here, but the frozen backend may still need a small find-path shim;
(b) any uvicorn/engineio dynamic import PyInstaller under-collects. Network
printers/terminals work regardless; USB-in-frozen-exe is the thing most
likely to need a follow-up. Iterate via the manual `build-exe-dev` channel.
"""

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# Packages PyInstaller tends to under-collect (dynamic imports / data files):
#   uvicorn        — loop/protocol autoloaders
#   zeroconf       — mDNS discovery
#   escpos         — capabilities.json data file
#   engineio/socketio — the uplink client's async drivers
#   certifi        — CA bundle main.py points SSL at
#   libusb_package — ships the libusb-1.0 DLL pyusb's backend needs on Windows
for pkg in ("uvicorn", "zeroconf", "escpos", "engineio", "socketio",
            "certifi", "libusb_package"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# Noto fonts + assets. bitmap_render.py resolves `src/assets/fonts` relative
# to `src/`, so preserve that exact layout inside the bundle.
datas += [("src/assets", "src/assets")]

hiddenimports += [
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "engineio.async_drivers.aiohttp",
    # pywin32 — escpos.printer.Win32Raw imports win32print lazily; PyInstaller
    # can't see that, so pull the modules in explicitly (its hooks then bundle
    # pywintypes/pythoncom DLLs). No-op on the Windows build if unused.
    "win32print",
    "win32api",
    "win32con",
    "pywintypes",
    # pyserial — serial/COM transport for USB terminals; list_ports has a
    # per-OS backend PyInstaller doesn't auto-detect.
    "serial",
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "emulator"],  # emulator is a dev tool, not shipped
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="bhm",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,   # default %TEMP%; can be pinned to the install dir later
    console=False,         # headless — no console window
)
