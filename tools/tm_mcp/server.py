"""MCP bridge to the TM2020 map editor.

Exposes the TMMapControl OpenPlanet plugin as MCP tools so a model
can drive the editor directly: place blocks, save, validate, inspect.

Why this exists: writing a ``.Map.Gbx`` with GBX.NET bypasses the
game's placement logic, so generated maps arrive with no support
pillars, default variants and no terrain adaptation. Reproducing
that logic externally meant reverse-engineering variant tables one
hand-placed block at a time. Placing through
``CGameEditorPluginMap`` instead makes the game do all of it, which
is both correct by construction and immune to game updates.

Transport is the same file-drop protocol the telemetry rig uses —
no ports, no elevation, works with the game sandboxed:

    <PluginStorage>/TMMapControl/<id>.cmd.json   written here
    <PluginStorage>/TMMapControl/<id>.res.json   written by plugin

Run:  python tools/tm_mcp/server.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - dependency hint
    print(
        "missing dependency: pip install 'mcp[cli]'",
        file=sys.stderr,
    )
    raise

PROTOCOL = "tm_mcp_v1"
DEFAULT_STORAGE = (
    Path(os.environ.get("USERPROFILE", Path.home()))
    / "OpenplanetNext" / "PluginStorage" / "TMMapControl"
)
STORAGE = Path(os.environ.get("TM_MCP_STORAGE", DEFAULT_STORAGE))

# The plugin polls every 400 ms; placement of a long route can take a
# while because each block goes through the editor's own pipeline.
DEFAULT_TIMEOUT_S = 120.0
POLL_S = 0.25

mcp = FastMCP("trackmania-editor")


class PluginTimeout(RuntimeError):
    pass


class PluginNotRunning(RuntimeError):
    pass


def _call(op: str, timeout: float = DEFAULT_TIMEOUT_S, **payload: Any) -> dict:
    """Drop a command file and wait for the plugin's response."""
    if not STORAGE.is_dir():
        raise PluginNotRunning(
            f"plugin storage not found: {STORAGE}. Is TM2020 running with "
            "the TMMapControl plugin loaded?"
        )
    cmd_id = uuid.uuid4().hex[:12]
    cmd_path = STORAGE / f"{cmd_id}.cmd.json"
    res_path = STORAGE / f"{cmd_id}.res.json"

    body = {"protocol": PROTOCOL, "op": op, **payload}
    cmd_path.write_text(json.dumps(body), encoding="utf-8")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if res_path.is_file():
            try:
                result = json.loads(res_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(POLL_S)  # plugin mid-write
                continue
            # Clean up our own pair; the plugin never deletes files.
            for p in (cmd_path, res_path):
                try:
                    p.unlink()
                except OSError:
                    pass
            return result
        time.sleep(POLL_S)

    try:
        cmd_path.unlink()
    except OSError:
        pass
    raise PluginTimeout(
        f"no response to '{op}' within {timeout:.0f}s. Is the game focused "
        "and the map editor open?"
    )


@mcp.tool()
def tm_state() -> dict:
    """Editor state: is it open, which map, how many blocks.

    Call this first — every other tool except this one requires the
    map editor to be open.
    """
    return _call("state", timeout=15.0)


@mcp.tool()
def tm_clear_blocks() -> dict:
    """Remove every block from the map currently open in the editor.

    Slow on a full map — the editor rebuilds as it goes — so this
    gets a generous timeout.
    """
    return _call("clear", timeout=300.0)


@mcp.tool()
def tm_place_blocks(blocks: list[dict]) -> dict:
    """Place blocks through the editor, letting the game finish the job.

    Each entry: ``{"name": "RoadTechStraight", "x": 20, "y": 9,
    "z": 20, "dir": 0}`` where ``dir`` is 0..3 (N/E/S/W).

    The returned ``blocks_total`` counts everything in the map after
    placement, so it includes support pillars the game generated on
    its own — compare it against ``len(blocks)`` to see how much the
    game added.
    """
    return _call("place_blocks", blocks=blocks, timeout=300.0)


@mcp.tool()
def tm_save_map(name: str = "") -> dict:
    """Save the open map, optionally renaming it first."""
    return _call("save", name=name, timeout=60.0)


@mcp.tool()
def tm_validate_map() -> dict:
    """Run the editor's own validation (drives the AI start to finish).

    Returns ``validation_status`` and, on success, ``author_time_ms``
    — the same gate TMX applies to uploads, so it is a real
    finishability check rather than our own approximation.
    """
    return _call("validate", timeout=300.0)


if __name__ == "__main__":
    mcp.run()
