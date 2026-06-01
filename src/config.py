import yaml
from pathlib import Path

# Install root is the parent of `src/` — that's where config.yaml lives.
# Resolving via __file__ instead of cwd means the manager works no matter
# where it's spawned from (launchctl bootstrap, runit, nohup from a
# shell script in $HOME, `python main.py` from anywhere).
_INSTALL_ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str = "config.yaml") -> dict:
    # Treat plain filenames as relative-to-install-root, but honour
    # absolute paths and cwd-relative paths the caller provides
    # explicitly (tests, alt-config setups).
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = _INSTALL_ROOT / p
    with open(p) as f:
        return yaml.safe_load(f)
