"""Write src/_uplink_secret.py from the UPLINK_SECRET environment variable.

Run by the build workflows before packaging, with the repository secret
`UPLINK_SECRET` in the environment. The generated file is git-ignored.
An unset secret writes nothing: the build connects without it.
"""

import os
import pathlib
import sys

secret = os.environ.get("UPLINK_SECRET", "").strip()
target = pathlib.Path(__file__).resolve().parent.parent / "src" / "_uplink_secret.py"
if not secret:
    print("::warning::UPLINK_SECRET is not set; this build sends no uplink secret")
    sys.exit(0)
target.write_text(f"UPLINK_SECRET = {secret!r}\n", encoding="utf-8")
print(f"uplink secret written to {target.name}")
