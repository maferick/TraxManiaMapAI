"""Diff our offline support pillars against what the game generates.

This closes the oracle loop. The offline emitter is the production
path (headless, batchable); the editor is only used here, as a
reference implementation to check that path against.

Flow:
  1. place the route through the editor -> the game adds its own
     pillars
  2. dump every block back out
  3. run ``build_supports`` on the same route offline
  4. diff the two pillar sets

Any disagreement is a bug in ``src/generation/supports.py``, printed
per-cell so it is actionable rather than a vibe.

Usage (TM2020 running, map editor open, TMMapControl loaded):
    python tools/verify_supports.py --seed 7 --length 60
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.catalogue.loader import load_catalogue
from src.generation.clip_walker import ClipWalker
from src.generation.families import resolve
from src.generation.priors import FacePriors
from src.generation.supports import PillarRules, build_supports

DEFAULT_STORAGE = (
    Path(os.environ.get("USERPROFILE", Path.home()))
    / "OpenplanetNext" / "PluginStorage" / "TMMapControl"
)
PROTOCOL = "tm_mcp_v1"
POLL_S = 0.25

ROADTECH_SET = [
    "RoadTechStart", "RoadTechFinish", "RoadTechCheckpoint",
    "RoadTechStraight", "RoadTechCurve1", "RoadTechCurve2",
    "RoadTechCurve3", "RoadTechChicaneX2Left", "RoadTechChicaneX2Right",
    "RoadTechChicaneX3Left", "RoadTechChicaneX3Right",
    "RoadTechSlopeBase", "RoadTechSlopeBase2",
]


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
    ap.add_argument("--catalogue", default="data/catalogue2/catalogue.ndjson")
    ap.add_argument("--priors", default="data/catalogue/face_priors.json")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--length", type=int, default=60)
    ap.add_argument("--checkpoint-every", type=int, default=15)
    ap.add_argument("--rules", default="data/catalogue/pillar_rules.json")
    ap.add_argument("--family", default=None,
                    help="surface family (tech/dirt/bump/ice/water); "
                         "default = the legacy hand-listed RoadTech set")
    args = ap.parse_args()

    storage = Path(args.storage)
    catalogue = load_catalogue(args.catalogue, collection="Stadium2020")
    priors = FacePriors.from_json(args.priors)
    if args.family:
        block_ids, clips = resolve([args.family], catalogue, max_footprint=3)
        walker = ClipWalker(catalogue, block_ids, seed=args.seed,
                            route_clips=clips, priors=priors)
    else:
        walker = ClipWalker(catalogue, ROADTECH_SET, seed=args.seed,
                            priors=priors)
    route = walker.generate(args.length, args.checkpoint_every)
    print(f"route: {len(route)} blocks")

    state = call(storage, "state", timeout=20.0)
    if not state.get("editor_open"):
        print("map editor is not open", file=sys.stderr)
        return 1

    call(storage, "clear")
    # Bottom-up: auto-pillars occupy cells, so an upper deck's pillar
    # can block a lower block where the route crosses itself.
    ordered = sorted(route, key=lambda p: (p.y, p.z, p.x))
    placed = call(storage, "place_blocks", blocks=[
        {"name": p.block_id, "x": p.x, "y": p.y, "z": p.z, "dir": p.rotation}
        for p in ordered
    ])
    print(f"placed {placed.get('placed')} / failed {placed.get('failed')}")
    if placed.get("failures"):
        print("  failures:", placed["failures"][:5])

    dump = call(storage, "dump_blocks", filter="Pillar")
    game = {
        (b["x"], b["y"], b["z"]): (b["name"], b["variant"], b["dir"])
        for b in dump.get("blocks", [])
    }
    print(f"game generated {len(game)} pillars")

    ours = {
        (p.x, p.y, p.z): (p.block_id, p.variant, p.rotation)
        for p in build_supports(route, catalogue, PillarRules.load(args.rules))
    }
    print(f"we generate   {len(ours)} pillars")

    missing = sorted(set(game) - set(ours))
    extra = sorted(set(ours) - set(game))
    both = set(game) & set(ours)
    name_mismatch = [c for c in both if game[c][0] != ours[c][0]]
    var_mismatch = [
        c for c in both
        if game[c][0] == ours[c][0] and game[c][1] != (ours[c][1] or 0)
    ]
    dir_mismatch = [
        c for c in both
        if game[c][0] == ours[c][0] and game[c][2] != ours[c][2]
    ]

    print()
    print(f"cells only in game : {len(missing)}")
    print(f"cells only in ours : {len(extra)}")
    print(f"same cell, wrong block   : {len(name_mismatch)}")
    print(f"same block, wrong variant: {len(var_mismatch)}")
    print(f"same block, wrong dir    : {len(dir_mismatch)}")

    for label, cells in (
        ("only in game", missing), ("only in ours", extra),
        ("wrong block", name_mismatch), ("wrong variant", var_mismatch),
        ("wrong dir", dir_mismatch),
    ):
        if not cells:
            continue
        print(f"\n{label} (first 6):")
        for c in cells[:6]:
            g = game.get(c)
            o = ours.get(c)
            print(f"  {c}  game={g}  ours={o}")

    if name_mismatch or var_mismatch or dir_mismatch:
        print("\nvariant counts by block, as the GAME writes them:")
        by = {}
        for name, var, _d in game.values():
            by.setdefault(name, Counter())[var] += 1
        for name, counter in sorted(by.items()):
            print(f"  {name:32s} {dict(counter)}")

    ok = not (missing or extra or name_mismatch or var_mismatch or dir_mismatch)
    print("\nMATCH" if ok else "\nMISMATCH — supports.py needs fixing")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
