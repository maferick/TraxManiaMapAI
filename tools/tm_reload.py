"""Reload an Openplanet plugin without asking a human to click.

Iterating on a plugin means a compile-error round trip, and each one
needed the operator to open Developer > Reload. This drives the
PluginReloader plugin over the same file-drop protocol everything else
here uses.

    python tools/tm_reload.py TMMapControl

The bootstrap is unavoidable: PluginReloader itself has to be loaded
by hand once, and it refuses to reload itself.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

STORAGE = Path(
    os.environ.get("USERPROFILE", Path.home())
) / "OpenplanetNext" / "PluginStorage" / "PluginReloader"


def reload_plugin(name: str, timeout: float = 30.0) -> dict:
    STORAGE.mkdir(parents=True, exist_ok=True)
    cmd_id = uuid.uuid4().hex[:12]
    cmd = STORAGE / f"{cmd_id}.cmd.json"
    res = STORAGE / f"{cmd_id}.res.json"
    cmd.write_text(json.dumps({"plugin": name}), encoding="utf-8")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if res.is_file():
            try:
                out = json.loads(res.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                time.sleep(0.2)
                continue
            for p in (cmd, res):
                try:
                    p.unlink()
                except OSError:
                    pass
            return out
        time.sleep(0.2)
    cmd.unlink(missing_ok=True)
    raise SystemExit(
        f"no response within {timeout:.0f}s — is PluginReloader loaded?"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python tools/tm_reload.py <PluginId>")
    print(json.dumps(reload_plugin(sys.argv[1]), indent=2))
