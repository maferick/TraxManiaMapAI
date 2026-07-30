"""Export FULL parsed maps as construction-token sequences.

The scaling path. The telemetry corpus taught real driven order but
capped training at 6,371 sequences from 880 played maps, while ~25k
fully-parsed maps sat unused because "driven order" had quietly become
a prerequisite for training at all. It never was one: driven order is
required for modelling the racing line, not for learning what a valid
map IS. For generation, serialization order is a free choice, and the
map file's own placement order (the mapper's build order, roughly) is
a perfectly good one.

What the in-game validation run of 2026-07-29 showed the telemetry-only
model cannot learn, and raw maps demonstrate on every single example:

* exactly one Start, at least one Finish (69% of NotValidable verdicts
  were missing-finish — inherited from telemetry runs that trail past
  the line onto scenery)
* only blocks that exist (one hallucinated name made a map unloadable)
* checkpoints that belong to a coherent track

Format: identical record shape to construction_sequences (map_uid +
tokens), so the trainer, sampler, realiser and transition scorer all
work unchanged. Raw sequences contain only P tokens -- REVISIT/JUMP/GAP
are traversal semantics and there is no traversal here.

Filters, each counted and reported:

* parse_status = 'success', Stadium2020 environment only
* every grid block name in the Stadium2020 catalogue -- maps carrying
  embedded/custom blocks are SKIPPED WHOLE, not hole-punched: dropping
  one block silently changes the map, which is how we'd end up training
  on maps that never existed
* exactly one Start waypoint and >= 1 Finish waypoint (catalogue
  `waypoint` field, NOT name substrings -- PlatformIceSlope2Start is a
  slope, a lesson already paid for once)
* block count <= --max-blocks. A HARD FILTER, deliberately not
  truncation: the trainer truncates at --max-len, and a map cut short
  mid-sequence teaches "end anywhere", which is precisely the corpus
  defect this export exists to remove. Maps must fit whole or stay out.

Run --stats first: it reports the block-count distribution and filter
kill counts so --max-blocks is chosen from data, not guessed.
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage.mariadb import open_connection  # noqa: E402
from src.utils.config import load_config  # noqa: E402

_LOG = logging.getLogger("raw-map-export")

EXPORT_VERSION = "raw_map-0.1"


def load_vocab(catalogue_path: Path) -> tuple[set[str], dict[str, str]]:
    """Stadium2020 block names + name -> waypoint kind."""
    names: set[str] = set()
    waypoint: dict[str, str] = {}
    with catalogue_path.open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("type") != "block":
                continue
            if r.get("collection") != "Stadium2020":
                continue
            names.add(r["name"])
            waypoint[r["name"]] = r.get("waypoint", "None")
    return names, waypoint


def candidate_maps(conn) -> list[tuple[int, str, str | None]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source_map_id, title
              FROM maps
             WHERE parse_status = 'success'
               AND environment = 'Stadium2020'
             ORDER BY id
            """
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def map_blocks(conn, map_id: int) -> list[dict]:
    """Grid blocks in file order. Free blocks are counted, not emitted."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT block_type, x, y, z, rotation, is_free
              FROM block_placements
             WHERE map_id = %s
             ORDER BY placement_index
            """,
            (map_id,),
        )
        return [
            {"block": r[0], "x": r[1], "y": r[2], "z": r[3],
             "rot": r[4], "free": bool(r[5])}
            for r in cur.fetchall()
        ]


def blocks_to_tokens(blocks: list[dict]) -> list[dict]:
    tokens = []
    prev = None
    for b in blocks:
        cur = (b["x"], b["y"], b["z"])
        d = [0, 0, 0] if prev is None else [
            cur[0] - prev[0], cur[1] - prev[1], cur[2] - prev[2]]
        tokens.append({
            "op": "PLACE", "block": b["block"], "d": d,
            "rot": int(b["rot"]) % 4, "free": False,
        })
        prev = cur
    return tokens


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalogue", type=Path,
                    default=Path("data/catalogue2/catalogue.ndjson"))
    ap.add_argument("--out", type=Path,
                    default=Path("data/artifacts/telemetry/"
                                 "raw_map_sequences_v0.1.jsonl"))
    ap.add_argument("--max-blocks", type=int, default=250,
                    help="skip maps with more grid blocks than this "
                         "(hard filter, never truncation)")
    ap.add_argument("--stats", action="store_true",
                    help="report distributions and filter kills, write "
                         "nothing")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N candidate maps (0 = all)")
    args = ap.parse_args()

    vocab, waypoint = load_vocab(args.catalogue)
    _LOG.info("vocabulary: %d Stadium2020 block types", len(vocab))

    conn = open_connection(load_config(None))
    maps = candidate_maps(conn)
    if args.limit:
        maps = maps[: args.limit]
    _LOG.info("candidates: %d parsed Stadium2020 maps", len(maps))

    kills = collections.Counter()
    block_counts: list[int] = []
    kept = 0
    out_fh = None
    if not args.stats:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        out_fh = args.out.open("w", encoding="utf-8")

    try:
        for i, (map_id, uid, title) in enumerate(maps):
            blocks = map_blocks(conn, map_id)
            grid = [b for b in blocks if not b["free"]]
            if not grid:
                kills["no_grid_blocks"] += 1
                continue
            block_counts.append(len(grid))

            unknown = [b["block"] for b in grid if b["block"] not in vocab]
            if unknown:
                kills["custom_or_unknown_block"] += 1
                continue

            kinds = [waypoint.get(b["block"], "None") for b in grid]
            n_start = sum(k == "Start" for k in kinds)
            n_finish = sum(k == "Finish" for k in kinds)
            # StartFinish is the multilap combined block; either shape
            # of a closed course is acceptable.
            n_both = sum(k == "StartFinish" for k in kinds)
            if not ((n_start == 1 and n_finish >= 1)
                    or (n_both == 1 and n_start == 0)):
                kills["bad_waypoint_structure"] += 1
                continue

            if len(grid) > args.max_blocks:
                kills["too_many_blocks"] += 1
                continue

            kept += 1
            if out_fh is not None:
                out_fh.write(json.dumps({
                    "version": EXPORT_VERSION,
                    "map_id": map_id,
                    "map_uid": uid,
                    "title": title,
                    "n_blocks": len(grid),
                    "tokens": blocks_to_tokens(grid),
                }, ensure_ascii=False) + "\n")

            if (i + 1) % 2000 == 0:
                _LOG.info("scanned %d/%d, kept %d", i + 1, len(maps), kept)
    finally:
        if out_fh is not None:
            out_fh.close()
        conn.close()

    print()
    print("candidates          %6d" % len(maps))
    for k, v in kills.most_common():
        print("killed: %-19s %6d" % (k, v))
    print("kept                %6d" % kept)
    if block_counts:
        s = sorted(block_counts)

        def pct(p):
            return s[min(len(s) - 1, int(p / 100 * len(s)))]

        print()
        print("grid blocks/map (pre-filter): "
              "p10=%d p25=%d p50=%d p75=%d p90=%d p95=%d max=%d"
              % (pct(10), pct(25), pct(50), pct(75), pct(90), pct(95),
                 s[-1]))
        for cap in (150, 250, 400, 600, 1000):
            fit = sum(1 for n in s if n <= cap)
            print("  maps fitting <= %4d blocks: %6d (%.0f%%)"
                  % (cap, fit, 100 * fit / len(s)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
