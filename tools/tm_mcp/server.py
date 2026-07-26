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
            except (json.JSONDecodeError, OSError):
                # Mid-write: partial JSON, or Windows holding an
                # exclusive lock while the plugin creates the file.
                time.sleep(POLL_S)
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
def tm_load_map(map_file: str) -> dict:
    """Open an existing .Map.Gbx in the editor.

    Use this to validate a generated ARTIFACT rather than a route
    re-placed by hand — it checks the file that would actually be
    played. ``map_file`` is a path the game can resolve, e.g.
    ``Maps/My Maps/whatever.Map.Gbx``.
    """
    return _call("load_map", map_file=map_file, timeout=180.0)


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
def tm_dump_blocks(filter: str = "") -> dict:
    """Read the open map back out of the editor.

    Returns every block as ``{name, x, y, z, dir, variant,
    mobil_variant, is_ground}``. ``filter`` keeps only names
    containing that substring.

    This is what makes the editor an oracle rather than a display.
    Two things it shows that no offline check can:

    * what the GAME added — place a route and dump, and the support
      pillars it generated are in the list
    * which mesh the game chose — ``mobil_variant`` changes when a
      road end is left unjoined, which is the dead-end barrier that
      offline geometry checks cannot see
    """
    return _call("dump_blocks", filter=filter, timeout=120.0)


@mcp.tool()
def tm_can_place(blocks: list[dict]) -> dict:
    """Ask the game whether placements are legal. Does not modify the map.

    Same entry shape as ``tm_place_blocks``:
    ``{"name": ..., "x": ..., "y": ..., "z": ..., "dir": 0..3}``.
    Returns a per-entry ``can_place`` plus a ``legal`` count.

    This is the game's own placement rule, which is the thing the
    offline generator has been approximating. Every structural bug so
    far — blocks meeting at a corner, a flat tile stepping up to a
    floating slab, two road ends that do not join — is a question this
    answers directly, per candidate, without shipping a map and
    looking at it.
    """
    return _call("can_place", blocks=blocks, timeout=180.0)


@mcp.tool()
def tm_validation_status() -> dict:
    """Read the map's validation status WITHOUT starting a validation run.

    ``tm_validate_map`` parks the editor waiting for a human driver
    and then times out. This just reports what the game already
    thinks, so it is safe unattended right after ``tm_load_map``:
    ``NotValidable`` is a real structural failure (missing
    Start/Finish, unlinked checkpoints), ``Validable`` means the
    topology is accepted.
    """
    return _call("status", timeout=30.0)


@mcp.tool()
def tm_camera(
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
    distance: float | None = None,
    h_angle: float | None = None,
    v_angle: float | None = None,
) -> dict:
    """Point the editor camera, so a screenshot shows what is being discussed.

    Position is in METRES, not grid cells: one cell is 32 m in X/Z and
    8 m in Y, and ground is grid row 9. Best effort — the orbital
    camera fields are undocumented and have moved between game builds,
    so the result reports which of them it managed to set.
    """
    payload: dict[str, Any] = {}
    if x is not None and y is not None and z is not None:
        payload.update(x=x, y=y, z=z)
    for key, value in (
        ("distance", distance), ("h_angle", h_angle), ("v_angle", v_angle),
    ):
        if value is not None:
            payload[key] = value
    if not payload:
        return {"ok": False, "error": "nothing to set"}
    return _call("camera", timeout=30.0, **payload)


@mcp.tool()
def tm_save_map(name: str = "") -> dict:
    """Save the open map, optionally renaming it first."""
    return _call("save", name=name, timeout=60.0)


@mcp.tool()
def tm_validate_map() -> dict:
    """Ask the editor to validate — REQUIRES A HUMAN TO DRIVE.

    TM2020 validation is not automated: the author drives the map
    start to finish and that run sets the author time. There is no AI
    driver. Calling this only moves the editor into validation; it
    then waits for a person, so an unattended call will time out.

    What the status still tells us, without anyone driving:

    * ``NotValidable`` — the map's TOPOLOGY is rejected (missing
      Start/Finish, unlinked checkpoints). A genuine structural
      failure, and useful.
    * ``Validable`` — structure accepted, awaiting a drive.
    * ``Validated`` — somebody completed a run; ``author_time_ms``
      is then meaningful.

    So this is a structure gate we can automate and a drivability
    gate we cannot. Offline, the closest proxy is that a clip-walked
    route closes Start->Finish by construction.
    """
    return _call("validate", timeout=300.0)


if __name__ == "__main__":
    mcp.run()
