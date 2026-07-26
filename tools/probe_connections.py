"""Ask the game, pair by pair, which block placements it actually accepts.

THREE DIFFERENT QUESTIONS, three different sources. Conflating them is
what produced every broken map so far, so they are kept apart here:

1. **Does the game permit this geometry?** Only the editor knows.
   ``PlaceBlock`` succeeds or fails, and that verdict covers footprint
   overlap, terrain rules and per-block placement constraints. This
   tool measures it.
2. **Do the two surfaces meet?** ``clip_matched`` in the placement
   grammar. The editor does NOT answer this — measured: a block's
   ``MobilVariantIndex`` is 0 whether it is joined end-to-end, sitting
   side by side unjoined, or completely isolated, and the editor
   happily places a block floating in mid-air with nothing touching
   it. Acceptance is not connection.
3. **Do real mappers do it?** ``map_count`` over 18,935 corpus maps.

The probe is a pair placed alone in a blank map: A at the probe
origin facing north, B at the grammar's offset and relative rotation.
Probes are spaced far enough apart that they cannot interact, 25 to a
round, and each round is undone with ``clear`` — which removes
exactly the blocks placed this session and nothing else.

The point is not to re-derive the grammar. It is to find where my
offline footprint and rotation model disagrees with the game, because
that model has been wrong twice before and a disagreement is a bug
with an address.

Output: ``data/catalogue/connection_probes.jsonl``, one row per pair,
consumable by the walker and by the description model.

Requires TM2020 with the TMMapControl plugin and a blank scratch map.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.catalogue.loader import load_catalogue, rotate_offset, rotate_vector
from src.generation.families import resolve_pool
from src.generation.grammar import PlacementGrammar
from tools.verify_map_in_game import call

_LOG = logging.getLogger("probe")

PROBE_VERSION = "connection-probes-v1"


class _Unattested:
    """A candidate pair the corpus never contains, shaped like a Move."""

    __slots__ = ("block", "offset", "rel_rotation", "map_count",
                 "clip_matched")

    def __init__(self, block, offset, rel_rotation):
        self.block = block
        self.offset = offset
        self.rel_rotation = rel_rotation
        self.map_count = 0
        self.clip_matched = False

# Probe spacing. The widest block in play is 3 cells and the widest
# offset is 3, so 7 guarantees two probes can never touch.
SPACING = 7
GRID_MIN, GRID_MAX = 6, 42
# Probe well ABOVE ground. At ground level every pair with dy < 0 puts
# B underground and the game refuses it — which looked like a pair
# rule in the first run and is really just "nothing below row 9".
# Measured: at BASE_Y 9 all seven refusals had dy of -1 or -2 and every
# dy >= 0 pair was accepted.
BASE_Y = 14
PER_ROUND = 25


def probe_origins() -> list[tuple[int, int]]:
    spots = []
    for x in range(GRID_MIN, GRID_MAX - SPACING, SPACING):
        for z in range(GRID_MIN, GRID_MAX - SPACING, SPACING):
            spots.append((x, z))
    return spots[:PER_ROUND]


def offline_ok(catalogue, a: str, b: str, offset, rel) -> bool:
    """What my own model predicts, so a disagreement is visible."""
    va = catalogue[a].variant("ground", 0)
    vb = catalogue[b].variant("ground", 0)
    if va is None or vb is None:
        return False
    cells_a = {rotate_offset(u.offset, 0, va.size) for u in va.units}
    cells_b = {
        tuple(
            c + d for c, d in zip(rotate_offset(u.offset, rel, vb.size), offset)
        )
        for u in vb.units
    }
    return not (cells_a & cells_b)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--families", default="dirt,platform-plastic,tech")
    ap.add_argument("--limit", type=int, default=400,
                    help="how many pairs to probe, strongest first")
    ap.add_argument("--min-maps", type=int, default=50)
    ap.add_argument("--catalogue", default="data/catalogue2/catalogue.ndjson")
    ap.add_argument("--grammar",
                    default="data/catalogue/placement_grammar.json")
    ap.add_argument("--scratch", required=True,
                    help="ABSOLUTE path to a blank .Map.Gbx to probe in "
                         "(load_map silently fails on relative paths)")
    ap.add_argument("--unattested", action="store_true",
                    help="probe pairs the corpus does NOT contain, to "
                         "see whether the game permits them anyway")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--base-y", type=int, default=BASE_Y,
                    help="probe altitude; must clear the deepest dy")
    ap.add_argument("--out", default="data/catalogue/connection_probes.jsonl")
    args = ap.parse_args()

    catalogue = load_catalogue(args.catalogue, collection="Stadium2020")
    grammar = PlacementGrammar.from_json(args.grammar)
    pool = resolve_pool(args.families.split(","), catalogue, max_footprint=3)
    allow = frozenset(pool)

    if args.unattested:
        # The other half of the question. Probing what the corpus
        # contains mostly confirms it: 99% of attested pairs are
        # accepted, so each one carries little information. What the
        # corpus does NOT contain is the interesting set — if the game
        # accepts those too, then editor acceptance is weak evidence
        # and the corpus is doing the real filtering, which decides how
        # much the generator should trust each source.
        import random

        rng = random.Random(args.seed)
        attested = {
            (a, m.block, m.offset, m.rel_rotation)
            for a in pool
            for m in grammar.successors(a, min_maps=1, allow=allow)
        }
        offsets = [
            (dx, dy, dz)
            for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
            if not (dx == 0 and dz == 0)
        ]
        candidates = [b for b in pool if b in grammar]
        pairs = []
        seen = set()
        while len(pairs) < args.limit and len(seen) < args.limit * 50:
            a = rng.choice(candidates)
            b = rng.choice(candidates)
            off = rng.choice(offsets)
            rel = rng.randrange(4)
            key = (a, b, off, rel)
            if key in seen:
                continue
            seen.add(key)
            if key in attested:
                continue
            pairs.append((a, _Unattested(b, off, rel)))
        _LOG.info("probing %d UNATTESTED pairs", len(pairs))
    else:
        # Strongest pairs first: those are the ones generation leans on.
        pairs = []
        for block_a in pool:
            for move in grammar.successors(
                block_a, min_maps=args.min_maps, allow=allow,
                overlays=False, gaps=False,
            ):
                pairs.append((block_a, move))
        pairs.sort(key=lambda p: -p[1].map_count)
        pairs = pairs[: args.limit]
        _LOG.info("probing %d attested pairs", len(pairs))

    state = call("state", timeout=20.0)
    if not state.get("ok"):
        _LOG.error("plugin not answering")
        return 1
    loaded = call("load_map", map_file=args.scratch, timeout=240.0)
    if not loaded.get("ok"):
        _LOG.error("could not load scratch map: %s", loaded.get("error"))
        return 1
    baseline = int(loaded.get("blocks", 0))
    _LOG.info("scratch map %r, %d blocks", loaded.get("map_name"), baseline)

    origins = probe_origins()
    rows: list[dict] = []
    for start in range(0, len(pairs), len(origins)):
        chunk = pairs[start: start + len(origins)]
        blocks: list[dict] = []
        placed_key: list[tuple] = []
        for (block_a, move), (ox, oz) in zip(chunk, origins):
            dx, dy, dz = move.offset
            ax, ay, az = ox, args.base_y, oz
            bx, by, bz = ax + dx, ay + dy, az + dz
            blocks.append({"name": block_a, "x": ax, "y": ay, "z": az,
                           "dir": 0})
            blocks.append({"name": move.block, "x": bx, "y": by, "z": bz,
                           "dir": move.rel_rotation})
            placed_key.append(((block_a, ax, ay, az), (move.block, bx, by, bz)))

        out = call("place_blocks", blocks=blocks, timeout=300.0)
        # Failures come back as "Name @x,y,z"; rebuild the same string.
        failed = set(out.get("failures", []))

        def bad(name, x, y, z):
            return f"{name} @{x},{y},{z}" in failed

        for (block_a, move), (ka, kb) in zip(chunk, placed_key):
            rows.append({
                "block_a": block_a,
                "block_b": move.block,
                "dx": move.offset[0], "dy": move.offset[1],
                "dz": move.offset[2],
                "rel_rotation": move.rel_rotation,
                "map_count": move.map_count,
                "clip_matched": bool(move.clip_matched),
                "editor_a_placed": not bad(*ka),
                "editor_b_placed": not bad(*kb),
                "offline_predicts_ok": offline_ok(
                    catalogue, block_a, move.block, move.offset,
                    move.rel_rotation),
            })
        _LOG.info(
            "round %d: %d pairs, placed=%s failed=%s",
            start // len(origins) + 1, len(chunk),
            out.get("placed"), out.get("failed"),
        )
        call("clear", timeout=300.0)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "meta", "schema": PROBE_VERSION,
            "families": args.families, "min_maps": args.min_maps,
            "base_y": args.base_y,
            "unattested": bool(args.unattested),
            "environment": "Stadium2020",
        }) + "\n")
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    accepted = sum(1 for r in rows if r["editor_a_placed"] and r["editor_b_placed"])
    disagree = [
        r for r in rows
        if (r["editor_a_placed"] and r["editor_b_placed"])
        != r["offline_predicts_ok"]
    ]
    _LOG.info(
        "%s: %d pairs, editor accepted %d (%.1f%%), "
        "offline model disagrees on %d -> %s",
        PROBE_VERSION, len(rows), accepted,
        100.0 * accepted / max(1, len(rows)), len(disagree), out_path,
    )
    for r in disagree[:15]:
        _LOG.warning(
            "DISAGREE %s -> %s off=(%d,%d,%d) rel=%d | editor=%s offline=%s",
            r["block_a"], r["block_b"], r["dx"], r["dy"], r["dz"],
            r["rel_rotation"],
            r["editor_a_placed"] and r["editor_b_placed"],
            r["offline_predicts_ok"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
