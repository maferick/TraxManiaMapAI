"""Harvest the game's auto-pillar rules, for use by the OFFLINE emitter.

Architecture note — this is the important part:

    The GBX writer (parsers/gbx-wrapper) is the PRODUCTION generator.
    It is headless and batch-capable, which the description->map goal
    depends on: you cannot require a running game with an open editor
    to generate a thousand candidate maps, train against them, or run
    generation server-side.

    The editor bridge (tools/tm_mcp) is NOT a runtime. It is an
    ORACLE. The game knows the pillar rules perfectly; we ask it once,
    write the answers down, and the offline emitter reproduces them
    forever after.

This script drives that harvest. For each road block, at a range of
heights, it:

  1. clears the map
  2. places the single block via the editor
  3. reads back every block the game added
  4. records the pillar block id / variant / direction per level

The output is a rule table (JSON) that ``src/generation/supports.py``
consumes, replacing the three-case guess derived from one
hand-placed reference.

Usage (TM2020 running, map editor open, TMMapControl loaded):
    python tools/harvest_pillar_rules.py --out data/catalogue/pillar_rules.json

The MCP server is not involved; this talks the same file-drop
protocol directly so it can run as a plain script.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

DEFAULT_STORAGE = (
    Path(os.environ.get("USERPROFILE", Path.home()))
    / "OpenplanetNext" / "PluginStorage" / "TMMapControl"
)
PROTOCOL = "tm_mcp_v1"
POLL_S = 0.25

# Blocks worth probing: one per distinct footprint/shape family the
# walker can emit. Heights chosen to expose foot / transition / shaft
# behaviour and any "short column" special case.
PROBE_BLOCKS = [
    "RoadTechStraight",
    "RoadTechCurve1",
    "RoadTechCurve2",
    "RoadTechCurve3",
    "RoadTechChicaneX2Left",
    "RoadTechChicaneX3Right",
    "RoadTechCheckpoint",
    "RoadTechSlopeBase",
]
PROBE_HEIGHTS = [10, 11, 12, 15]
GROUND_Y = 9


def call(storage: Path, op: str, timeout: float = 120.0, **payload) -> dict:
    cmd_id = uuid.uuid4().hex[:12]
    cmd = storage / f"{cmd_id}.cmd.json"
    res = storage / f"{cmd_id}.res.json"
    cmd.write_text(json.dumps({"protocol": PROTOCOL, "op": op, **payload}),
                   encoding="utf-8")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if res.is_file():
            try:
                out = json.loads(res.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(POLL_S)
                continue
            for p in (cmd, res):
                try:
                    p.unlink()
                except OSError:
                    pass
            return out
        time.sleep(POLL_S)
    try:
        cmd.unlink()
    except OSError:
        pass
    raise TimeoutError(f"no response to {op!r} within {timeout:.0f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--storage", default=str(DEFAULT_STORAGE))
    ap.add_argument("--out", default="data/catalogue/pillar_rules.json")
    ap.add_argument("--x", type=int, default=20)
    ap.add_argument("--z", type=int, default=20)
    args = ap.parse_args()

    storage = Path(args.storage)
    if not storage.is_dir():
        print(f"plugin storage not found: {storage}", file=sys.stderr)
        print("Is TM2020 running with TMMapControl loaded?", file=sys.stderr)
        return 1

    state = call(storage, "state", timeout=20.0)
    if not state.get("editor_open"):
        print("map editor is not open", file=sys.stderr)
        return 1
    print(f"editor ready, baseline blocks={state.get('blocks')}")

    rules: dict[str, dict] = {}
    for block in PROBE_BLOCKS:
        rules[block] = {}
        for height in PROBE_HEIGHTS:
            call(storage, "clear", timeout=300.0)
            placed = call(
                storage, "place_blocks", timeout=120.0,
                blocks=[{"name": block, "x": args.x, "y": height,
                         "z": args.z, "dir": 0}],
            )
            if placed.get("failed"):
                print(f"  {block} @y={height}: placement failed, skipped")
                continue
            dump = call(storage, "dump_blocks", timeout=120.0)
            added = [
                b for b in dump.get("blocks", [])
                if "Pillar" in b.get("name", "")
            ]
            added.sort(key=lambda b: b.get("y", 0))
            rules[block][str(height)] = [
                {
                    "dy": b["y"] - GROUND_Y,
                    "name": b["name"],
                    "variant": b.get("variant"),
                    "dir": b.get("dir"),
                    "dx": b["x"] - args.x,
                    "dz": b["z"] - args.z,
                }
                for b in added
            ]
            print(f"  {block} @y={height}: {len(added)} pillars")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "schema": "pillar_rules_v1",
        "ground_y": GROUND_Y,
        "rules": rules,
    }, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
