"""Ask the mined grammar what the corpus puts next to a block.

The point of ``mine-placement-grammar`` is that it answers questions
about real maps that no rule of mine can. This is how to ask them.

    python tools/grammar_query.py RoadTechStraight
    python tools/grammar_query.py GateCheckpoint --no-clip
    python tools/grammar_query.py PlatformPlasticBase --min-maps 200

``--no-clip`` shows only pairs the clip model rejects — jumps,
overlays and cross-family joins, i.e. everything the walker used to
consider impossible.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage.mariadb import cursor, open_connection
from src.utils.config import load_config

_SQL = """
SELECT block_b, dx, dy, dz, rel_rotation, clip_matched, map_count, pair_count
FROM block_placement_grammar
WHERE block_a = %s AND environment = %s AND map_count >= %s
{clip_clause}
ORDER BY map_count DESC
LIMIT %s
"""


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("block")
    ap.add_argument("--environment", default="Stadium2020")
    ap.add_argument("--min-maps", type=int, default=10)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--no-clip", action="store_true",
                    help="only pairs with no clip match (the new ground)")
    ap.add_argument("--clip-only", action="store_true")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    clip_clause = ""
    if args.no_clip:
        clip_clause = "AND clip_matched = 0"
    elif args.clip_only:
        clip_clause = "AND clip_matched = 1"

    conn = open_connection(load_config(args.config))
    try:
        with cursor(conn) as cur:
            cur.execute(
                _SQL.format(clip_clause=clip_clause),
                (args.block, args.environment, args.min_maps, args.top),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"no grammar rows for {args.block!r}")
        return 1

    print(f"{'block_b':44} {'offset':14} {'rel':>3} {'clip':>4} "
          f"{'maps':>7} {'pairs':>9}")
    for b, dx, dy, dz, rel, clip, maps, pairs in rows:
        off = f"({dx:>2},{dy:>2},{dz:>2})"
        print(f"{b:44} {off:14} {rel:>3} {clip:>4} {maps:>7} {pairs:>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
