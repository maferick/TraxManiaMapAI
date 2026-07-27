"""Run driven-path extraction over captured trajectories and validate.

Validation is the part that matters. The Viterbi output looks plausible
by construction, so plausibility proves nothing. What it is checked
against instead:

* checkpoint consistency -- at every checkpoint crossing (anchored via
  the ghost's own split times), the trajectory must be on or beside a
  waypoint-bearing cell. The splits come from the ghost's race result,
  a fully independent measurement.
* teleport count -- a correct sequence has approximately as many
  non-adjacent block transitions as the ghost had respawns (usually 0).
* off-surface accounting -- unexplained stretches are labelled, not
  absorbed into the nearest block.

Inputs are the same file exports the coverage tool uses, plus a
waypoints TSV:

    python tools/extract_driven_path.py \\
        --placements pilot_blocks2.tsv --path-map path_to_uid.json \\
        --telemetry-dir telemetry_pilot --waypoints waypoints.tsv
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.catalogue.loader import load_catalogue  # noqa: E402
from src.route.block_matcher import Placement, to_cell  # noqa: E402
from src.route.driven_path import extract_driven_path  # noqa: E402

_LOG = logging.getLogger("driven-path")


def load_placements(tsv: Path, path_map: dict[str, str]):
    by_map = defaultdict(list)
    for i, line in enumerate(tsv.open(encoding="utf-8", errors="replace")):
        f = line.rstrip("\n").split("\t")
        if len(f) < 14:
            continue
        uid = path_map.get(f[0])
        if uid is None:
            continue
        gx, gy, gz = int(f[4]), int(f[5]), int(f[6])
        ax, ay, az = float(f[8]), float(f[9]), float(f[10])
        by_map[uid].append(Placement(
            index=i, block_type=f[1],
            variant=int(f[2]) if f[2] not in ("", "NULL") else 0,
            is_free=(f[3] == "1"),
            x=None if gx == -999 else gx, y=None if gy == -999 else gy,
            z=None if gz == -999 else gz,
            rotation=int(f[7]) if f[7] not in ("", "NULL") else 0,
            abs_x=None if ax == -999 else ax,
            abs_y=None if ay == -999 else ay,
            abs_z=None if az == -999 else az,
            yaw=float(f[11]), pitch=float(f[12]), roll=float(f[13]),
        ))
    return by_map


def load_waypoints(tsv: Path):
    """uid -> {cell: tag}. Free/abs waypoints are converted to cells."""
    out: dict[str, dict[tuple[int, int, int], str]] = defaultdict(dict)
    for line in tsv.open(encoding="utf-8", errors="replace"):
        f = line.rstrip("\n").split("\t")
        if len(f) < 9:
            continue
        uid, tag = f[0], f[1]
        gx, gy, gz = int(f[3]), int(f[4]), int(f[5])
        ax, ay, az = float(f[6]), float(f[7]), float(f[8])
        if gx != -999 and gx < 255:
            out[uid][(gx, gy, gz)] = tag
        elif ax != -999:
            out[uid][to_cell(ax, ay, az)] = tag
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--placements", type=Path, required=True)
    ap.add_argument("--path-map", type=Path, required=True)
    ap.add_argument("--telemetry-dir", type=Path, required=True)
    ap.add_argument("--waypoints", type=Path, default=None)
    ap.add_argument("--catalogue", type=Path,
                    default=Path("data/catalogue2/catalogue.ndjson"))
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="write per-map visit sequences as JSON")
    args = ap.parse_args()

    catalogue = load_catalogue(args.catalogue, collection="Stadium2020")
    path_map = json.loads(args.path_map.read_text())
    by_map = load_placements(args.placements, path_map)
    waypoints = load_waypoints(args.waypoints) if args.waypoints else {}

    print("%-30s %6s %7s %8s %6s %9s %8s %9s" % (
        "map", "visits", "blocks", "distinct", "cp", "teleport",
        "offsurf", "offsurf_s"))
    print("-" * 100)

    total_cp = total_hit = 0
    for uid in sorted(by_map):
        tele = args.telemetry_dir / f"{uid}.telemetry.json"
        if not tele.is_file():
            continue
        doc = json.loads(tele.read_text())
        if not (doc.get("extra") or {}).get("clock_rebased_to_race_start"):
            continue
        path = extract_driven_path(
            doc["samples"], by_map[uid], catalogue,
            checkpoint_indices=doc.get("checkpoint_sample_indices") or (),
            waypoint_cells=waypoints.get(uid),
        )
        s = path.stats
        cp = ("%d/%d" % (path.checkpoint_hits, path.checkpoint_total)
              if path.checkpoint_total else "-")
        total_cp += path.checkpoint_total
        total_hit += path.checkpoint_hits
        print("%-30s %6d %7d %8d %6s %9d %8d %9.1f" % (
            uid[:30], s["visits"], s["block_visits"], s["distinct_blocks"],
            cp, s["teleports"], s["off_surface_visits"],
            s["off_surface_ms"] / 1000))

        if args.out_dir:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            (args.out_dir / f"{uid}.visits.json").write_text(json.dumps({
                "map_uid": uid,
                "version": "driven_path-0.1",
                "visits": [
                    {
                        "block_type": v.block_type,
                        "placement_index": v.state if v.state >= 0 else None,
                        "first_sample": v.first_sample,
                        "last_sample": v.last_sample,
                        "enter_ms": v.enter_ms,
                        "exit_ms": v.exit_ms,
                    }
                    for v in path.visits
                ],
            }, indent=1), encoding="utf-8")

    print("-" * 100)
    if total_cp:
        print("CHECKPOINT CONSISTENCY %d/%d = %.1f%%"
              % (total_hit, total_cp, 100 * total_hit / total_cp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
