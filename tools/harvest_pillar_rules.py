"""Harvest the game's auto-pillar rules, for use by the OFFLINE emitter.

Architecture note — this is the important part:

    The GBX writer (parsers/gbx-wrapper) is the PRODUCTION generator.
    It is headless and batch-capable, which the description->map goal
    depends on: you cannot require a running game with an open editor
    to produce a thousand candidates, train against them, or generate
    server-side.

    The editor bridge (tools/tm_mcp) is NOT a runtime. It is an
    ORACLE. The game knows the pillar rules perfectly; we ask it once,
    write the answers down, and the offline emitter reproduces them
    forever after.

Scale: ~3.7k Stadium2020 blocks expose side clips, so probing one at
a time (clear/place/dump per block) would run for hours. Two
observations make it ~25 minutes instead:

  * the pillar's block id, variant and direction are constant across
    heights (verified: straights are variant 0 at every level, curves
    variant 1), so ONE height per block is enough
  * isolated blocks do not interact, so a whole grid of them can be
    placed and dumped in a single round trip

Pillars are attributed to the probe block whose grid slot they fall
in; slots are spaced wider than the largest footprint (Curve5, 5x5).

Usage (TM2020 running, map editor open, TMMapControl loaded):
    python tools/harvest_pillar_rules.py --out data/catalogue/pillar_rules.json
    python tools/harvest_pillar_rules.py --family RoadDirt --limit 40
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_STORAGE = (
    Path(os.environ.get("USERPROFILE", Path.home()))
    / "OpenplanetNext" / "PluginStorage" / "TMMapControl"
)
PROTOCOL = "tm_mcp_v1"
POLL_S = 0.25

GROUND_Y = 9
# One probe height. Three levels of pillar is enough to see the block
# id / variant / direction and confirm the column is uniform.
PROBE_Y = 12
# Grid slots, wider than the largest footprint (Curve5 = 5x5) so
# neighbouring probes cannot share or block each other's pillars.
SLOT = 8
SLOT_ORIGIN = 6
SLOTS_PER_AXIS = 5  # 6,14,22,30,38 inside a 48x48 map


def call(storage: Path, op: str, timeout: float = 300.0, **payload) -> dict:
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
            except (json.JSONDecodeError, OSError):
                # Mid-write: partial JSON, or Windows holding a lock.
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


def candidate_blocks(catalogue_path: str, family: str | None) -> list[str]:
    """Drivable, non-pillar Stadium2020 blocks — those a route can use."""
    out = []
    with open(catalogue_path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("type") != "block":
                continue
            if rec.get("collection") != "Stadium2020":
                continue
            block_id = rec["id"]
            if rec.get("is_pillar") or "Pillar" in block_id:
                continue
            if family and not block_id.startswith(family):
                continue
            variants = [
                v for v in rec.get("variants", [])
                if v.get("kind") == "ground" and v.get("index") == 0
            ]
            if not variants:
                continue
            has_side = any(
                unit.get("clips", {}).get(face)
                for unit in variants[0].get("units", [])
                for face in ("n", "e", "s", "w")
            )
            if has_side:
                out.append(block_id)
    return sorted(out)


def slots() -> list[tuple[int, int]]:
    return [
        (SLOT_ORIGIN + SLOT * i, SLOT_ORIGIN + SLOT * j)
        for i in range(SLOTS_PER_AXIS)
        for j in range(SLOTS_PER_AXIS)
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--storage", default=str(DEFAULT_STORAGE))
    ap.add_argument("--catalogue", default="data/catalogue2/catalogue.ndjson")
    ap.add_argument("--out", default="data/catalogue/pillar_rules.json")
    ap.add_argument("--family", default=None,
                    help="restrict to a block-id prefix, e.g. RoadDirt")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    storage = Path(args.storage)
    if not storage.is_dir():
        print(f"plugin storage not found: {storage}", file=sys.stderr)
        return 1
    state = call(storage, "state", timeout=20.0)
    if not state.get("editor_open"):
        print("map editor is not open", file=sys.stderr)
        return 1

    blocks = candidate_blocks(args.catalogue, args.family)
    if args.limit:
        blocks = blocks[:args.limit]
    grid = slots()
    batches = [blocks[i:i + len(grid)] for i in range(0, len(blocks), len(grid))]
    print(f"{len(blocks)} blocks in {len(batches)} batches of <= {len(grid)}")

    rules: dict[str, dict] = {}
    unplaceable: list[str] = []
    started = time.monotonic()

    for bi, batch in enumerate(batches, 1):
        call(storage, "clear")
        placement = []
        where: dict[str, tuple[int, int]] = {}
        for block_id, (px, pz) in zip(batch, grid):
            placement.append({"name": block_id, "x": px, "y": PROBE_Y,
                              "z": pz, "dir": 0})
            where[block_id] = (px, pz)
        placed = call(storage, "place_blocks", blocks=placement)

        dump = call(storage, "dump_blocks", filter="Pillar")
        pillars = dump.get("blocks", [])

        for block_id, (px, pz) in where.items():
            mine = [
                p for p in pillars
                if abs(p["x"] - px) < SLOT // 2 + 1
                and abs(p["z"] - pz) < SLOT // 2 + 1
            ]
            if not mine:
                unplaceable.append(block_id)
                continue

            # A block may get SEVERAL pillars per level, one per
            # footprint cell, and they can differ: a
            # RoadTechToRoadBump transition puts dir=2 under its tech
            # end and dir=0 under its bump end. So the unit of a rule
            # is the per-level PATTERN, not a single pillar. v2
            # recorded only the bottom pillar and flagged the rest
            # "non-uniform", which lost the information.
            by_level: dict[int, list[dict]] = {}
            for p in mine:
                by_level.setdefault(p["y"], []).append(p)
            levels = sorted(by_level)

            def signature(entries: list[dict]) -> list[tuple]:
                return sorted(
                    (e["x"] - px, e["z"] - pz, e["name"], e["variant"],
                     e["dir"])
                    for e in entries
                )

            base_sig = signature(by_level[levels[0]])
            vertically_uniform = all(
                signature(by_level[y]) == base_sig for y in levels
            )
            rules[block_id] = {
                "pattern": [
                    {"dx": dx, "dz": dz, "pillar": name,
                     "variant": variant, "dir": direction}
                    for dx, dz, name, variant, direction in base_sig
                ],
                "levels": len(levels),
                # True when every level repeats the bottom pattern,
                # which is what lets the emitter stamp it upward.
                "uniform": vertically_uniform,
            }

        done = bi * len(grid)
        rate = (time.monotonic() - started) / bi
        eta = rate * (len(batches) - bi) / 60
        print(f"  batch {bi}/{len(batches)}  placed={placed.get('placed')} "
              f"failed={placed.get('failed')}  learned={len(rules)}  "
              f"eta {eta:.0f}m")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "schema": "pillar_rules_v3",
        "ground_y": GROUND_Y,
        "probe_y": PROBE_Y,
        "rules": rules,
        "no_pillars": sorted(set(unplaceable)),
    }, indent=2), encoding="utf-8")

    non_uniform = [k for k, v in rules.items() if not v["uniform"]]
    print(f"\nlearned {len(rules)} rules, "
          f"{len(set(unplaceable))} blocks generated no pillars")
    if non_uniform:
        print(f"NON-UNIFORM columns ({len(non_uniform)}): "
              f"{non_uniform[:8]} — these need per-level handling")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
