"""PET-971 — the offline runtime the installers ship, laid out by
scripts/build_offline_runtime.sh. A fake Node release (file://) stands in for
nodejs.org: the layout, the renaming and the checksum check are what matter.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_offline_runtime.sh"
VERSION = "v22.11.0"


def _release(tmp: Path, platform: str, tamper: bool = False) -> Path:
    dist = tmp / "dist" / VERSION
    dist.mkdir(parents=True)
    if platform == "win-x64":
        name = f"node-{VERSION}-win-x64.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(f"node-{VERSION}-win-x64/node.exe", b"MZ fake node")
        data = buf.getvalue()
    else:
        name = f"node-{VERSION}-{platform}.tar.gz"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            body = b"#!/bin/sh\necho fake node\n"
            info = tarfile.TarInfo(f"node-{VERSION}-{platform}/bin/node")
            info.size = len(body)
            info.mode = 0o755
            t.addfile(info, io.BytesIO(body))
        data = buf.getvalue()
    (dist / name).write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    if tamper:
        digest = "0" * 64
    (dist / "SHASUMS256.txt").write_text(f"{digest}  {name}\n" + f"{'1' * 64}  other.tar.gz\n")
    return tmp / "dist"


def _inputs(tmp: Path) -> tuple[Path, Path]:
    service = tmp / "service"
    (service / "dist").mkdir(parents=True)
    (service / "dist" / "main.js").write_text("console.log('service')")
    (service / "package.json").write_text('{\n  "name": "petshandler-offline",\n  "version": "0.3.1"\n}\n')
    app = tmp / "app"
    (app / "assets").mkdir(parents=True)
    (app / "index.html").write_text("<div id=app></div>")
    (app / "assets" / "a.js").write_text("x")
    return service, app


def _run(tmp: Path, platform: str, base: Path) -> subprocess.CompletedProcess:
    service, app = _inputs(tmp)
    env = {**os.environ, "NODE_VERSION": VERSION, "NODE_DIST_BASE": base.as_uri(), "TMPDIR": str(tmp)}
    return subprocess.run(
        ["bash", str(SCRIPT), platform, str(tmp / "out"), str(service), str(app)],
        capture_output=True, text=True, env=env, timeout=60,
    )


@pytest.mark.parametrize(
    "platform,binary",
    [("darwin-arm64", "device-handler-offline"), ("win-x64", "device-handler-offline.exe")],
)
def test_lays_out_node_the_service_and_the_app(tmp_path, platform, binary):
    r = _run(tmp_path, platform, _release(tmp_path, platform))
    assert r.returncode == 0, r.stdout + r.stderr
    out = tmp_path / "out"
    assert (out / binary).is_file()
    assert (out / "service" / "main.js").read_text() == "console.log('service')"
    # The manager checks /health for exactly this.
    assert (out / "service" / "version.txt").read_text().strip() == "0.3.1"
    assert (out / "app" / "index.html").is_file() and (out / "app" / "assets" / "a.js").is_file()
    if platform != "win-x64":
        assert os.access(out / binary, os.X_OK)


def test_a_node_archive_that_does_not_match_its_checksum_is_refused(tmp_path):
    r = _run(tmp_path, "darwin-arm64", _release(tmp_path, "darwin-arm64", tamper=True))
    assert r.returncode != 0
    assert "checksum mismatch" in r.stdout + r.stderr
    assert not (tmp_path / "out" / "device-handler-offline").exists()


def test_the_manager_finds_the_runtime_where_the_script_puts_it(tmp_path):
    from src.services.offline_service import node_path, service_script, shipped_version

    r = _run(tmp_path, "darwin-arm64", _release(tmp_path, "darwin-arm64"))
    assert r.returncode == 0, r.stderr
    out = tmp_path / "out"
    assert node_path(out).is_file()
    assert service_script(out).is_file()
    assert shipped_version(out) == "0.3.1"


# ---- the release pipeline --------------------------------------------------

import yaml


@pytest.mark.parametrize(
    "workflow,windows,mac",
    [
        (".github/workflows/publish.yml", "build-windows-exe", "build-macos-app"),
        (".github/workflows/build-exe-dev.yml", "build-windows", "build-macos"),
    ],
)
def test_both_installers_carry_the_runtime_when_its_secrets_are_set(workflow, windows, mac):
    jobs = yaml.safe_load((ROOT / workflow).read_text())["jobs"]
    for job in (windows, mac):
        steps = jobs[job]["steps"]
        assert jobs[job]["env"]["OFFLINE_REPO_TOKEN"] == "${{ secrets.OFFLINE_REPO_TOKEN }}"
        uses = [s for s in steps if s.get("uses") == "./.github/actions/offline-runtime"]
        assert len(uses) == 1, job
        # Only with the secret: a release without it builds as before.
        assert uses[0]["if"] == "${{ env.OFFLINE_REPO_TOKEN != '' }}"
    win_steps = jobs[windows]["steps"]
    names = [s.get("name") for s in win_steps]
    assert names.index("Offline runtime") < names.index("Build installer (Inno Setup)")
    iscc = next(s for s in win_steps if s.get("name") == "Build installer (Inno Setup)")["run"]
    assert "@offline installers\\barhandler-setup.iss" in iscc
    mac_steps = jobs[mac]["steps"]
    mac_names = [s.get("name") for s in mac_steps]
    sign = next(n for n in mac_names if n and n.startswith("Sign"))
    # Inside the .app before it is signed.
    assert mac_names.index("Offline runtime") < mac_names.index(sign)
    runtime = next(s for s in mac_steps if s.get("name") == "Offline runtime")
    assert runtime["with"]["out"].startswith("dist/Device Handler.app/Contents/Resources/offline-runtime/")


def test_the_mac_script_signs_the_nested_node_before_the_app():
    script = (ROOT / "scripts" / "mac_sign_and_package.sh").read_text()
    node = script.index("entitlements-offline-node.plist")
    app = script.index('--entitlements "$ENTITLEMENTS"')
    assert node < app
