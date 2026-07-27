"""Measure how much captured telemetry resolves to candidate blocks.

This is the gate that decides whether a map's telemetry is usable for
route inference. It runs both matching paths (grid footprints and free
placements) and reports what is left unexplained.

Coverage, not correctness: a sample "covered" here means at least one
block could plausibly hold the car. Picking the right one is the
Viterbi step's job.

Airborne samples are reported separately and excluded from the coverage
denominator. A car mid-jump is not on a block, so counting those as
misses would penalise exactly the maps with the most interesting
geometry. The airborne test is kinematic (ballistic vertical
acceleration) and never consults block data, so it cannot explain away
a genuine matching failure.

Placements come from a TSV export, produced by:

    SELECT m.raw_artifact_path, b.block_type, IFNULL(b.variant,''),
           b.is_free, IFNULL(b.x,-999), IFNULL(b.y,-999),
           IFNULL(b.z,-999), b.rotation,
           IFNULL(b.abs_x,-999), IFNULL(b.abs_y,-999),
           IFNULL(b.abs_z,-999),
           IFNULL(b.yaw,0), IFNULL(b.pitch,0), IFNULL(b.roll,0)
      FROM block_placements b JOIN maps m ON m.id = b.map_id
     WHERE m.raw_artifact_path IN (...);

Usage:

    python tools/telemetry_block_coverage.py \\
        --placements pilot_blocks2.tsv \\
        --path-map path_to_uid.json \\
        --telemetry-dir telemetry_pilot \\
        --catalogue data/catalogue2/catalogue.ndjson
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.catalogue.loader import load_catalogue  # noqa: E402
from src.route.block_matcher import (  # noqa: E402
    CandidateIndex,
    Placement,
    classify_airborne,
)

_LOG = logging.getLogger("coverage")

# Vertical slack when asking whether a sample sits on a footprint cell.
# The measured offset histogram peaks at 0 but has a -1 tail (17.4%),
# which is what a car riding the top surface of a block, or a sloped
# block, produces. One row is candidate generation, not a fudge.
ROW_TOLERANCE = (0, -1)


def load_placements(tsv: Path, path_map: dict[str, str]):
    by_map = defaultdict(list)
    skipped = 0
    for i, line in enumerate(tsv.open(encoding="utf-8", errors="replace")):
        f = line.rstrip("\n").split("\t")
        if len(f) < 14:
            skipped += 1
            continue
        uid = path_map.get(f[0])
        if uid is None:
            continue
        try:
            variant = int(f[2]) if f[2] not in ("", "NULL") else 0
        except ValueError:
            variant = 0
        is_free = f[3] == "1"
        gx, gy, gz = (int(f[4]), int(f[5]), int(f[6]))
        ax, ay, az = (float(f[8]), float(f[9]), float(f[10]))
        by_map[uid].append(Placement(
            index=i,
            block_type=f[1],
            variant=variant,
            is_free=is_free,
            x=None if gx == -999 else gx,
            y=None if gy == -999 else gy,
            z=None if gz == -999 else gz,
            rotation=int(f[7]) if f[7] not in ("", "NULL") else 0,
            abs_x=None if ax == -999 else ax,
            abs_y=None if ay == -999 else ay,
            abs_z=None if az == -999 else az,
            yaw=float(f[11]), pitch=float(f[12]), roll=float(f[13]),
        ))
    if skipped:
        _LOG.warning("skipped %d malformed placement rows", skipped)
    return by_map


def analyse(uid, samples, index, cp_indices=(), cp_window=10):
    air = classify_airborne(samples)
    n = len(samples)
    res = {
        "samples": n,
        "airborne": sum(air),
        "grounded": 0,
        "grid": 0, "free": 0, "either": 0,
        "air_covered": 0,
    }
    covered = [False] * n
    for i, s in enumerate(samples):
        x, y, z = s["x"], s["y"], s["z"]
        g = any(index.grid_hit(x, y, z, dy=d) for d in ROW_TOLERANCE)
        fr = index.free_hit(x, y, z) if not g else False
        covered[i] = g or fr
        if air[i]:
            if g or fr:
                res["air_covered"] += 1
            continue
        res["grounded"] += 1
        if g:
            res["grid"] += 1
        if fr:
            res["free"] += 1
        if g or fr:
            res["either"] += 1

    # Longest run of grounded-and-unmatched samples, plus how far the
    # car travels during it. A long gap is a real hole in the map data;
    # scattered singles are noise at the edges of footprints.
    best_len = best_dist = 0
    cur_len = cur_dist = 0
    prev = None
    for i, s in enumerate(samples):
        bad = (not air[i]) and (not covered[i])
        if bad:
            cur_len += 1
            if prev is not None:
                cur_dist += math.dist(
                    (s["x"], s["y"], s["z"]),
                    (prev["x"], prev["y"], prev["z"]),
                )
            if cur_len > best_len:
                best_len, best_dist = cur_len, cur_dist
        else:
            cur_len = cur_dist = 0
        prev = s
    res["gap_samples"] = best_len
    res["gap_m"] = best_dist

    # Checkpoint and finish regions get their own number. Those are the
    # anchors route inference is pinned to, so coverage there matters
    # more than average coverage: a hole in open track costs one
    # uncertain stretch, a hole at a checkpoint breaks the pinning.
    cp_tot = cp_cov = 0
    for ci in cp_indices:
        for j in range(max(0, ci - cp_window), min(n, ci + cp_window + 1)):
            if air[j]:
                continue
            cp_tot += 1
            cp_cov += 1 if covered[j] else 0
    res["cp_samples"] = cp_tot
    res["cp_covered"] = cp_cov
    return res, covered, air


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--placements", type=Path, required=True)
    ap.add_argument("--path-map", type=Path, required=True)
    ap.add_argument("--telemetry-dir", type=Path, required=True)
    ap.add_argument("--catalogue", type=Path,
                    default=Path("data/catalogue2/catalogue.ndjson"))
    ap.add_argument("--free-anchor", choices=("corner", "center"),
                    default="corner")
    ap.add_argument("--free-pad", type=float, default=0.0,
                    help="metres of slack on free bounding volumes")
    args = ap.parse_args()

    catalogue = load_catalogue(args.catalogue, collection="Stadium2020")
    path_map = json.loads(args.path_map.read_text())
    by_map = load_placements(args.placements, path_map)

    tot = defaultdict(int)
    rows = []
    unknown = set()
    for uid, placements in sorted(by_map.items()):
        f = args.telemetry_dir / f"{uid}.telemetry.json"
        if not f.is_file():
            continue
        doc = json.loads(f.read_text())
        samples = doc["samples"]
        cps = doc.get("checkpoint_sample_indices") or []
        index = CandidateIndex(
            placements, catalogue,
            free_anchor=args.free_anchor, free_pad_m=args.free_pad,
        )
        unknown |= index.unknown_blocks
        res, _, _ = analyse(uid, samples, index, cp_indices=cps)
        res.update(uid=uid, n_grid=index.n_grid, n_free=index.n_free,
                   cells=index.footprint_cells)
        rows.append(res)
        for k in ("samples", "airborne", "grounded", "grid", "free",
                  "either", "air_covered", "cp_samples", "cp_covered"):
            tot[k] += res[k]

    rows.sort(key=lambda r: r["either"] / max(1, r["grounded"]))
    print()
    print("%-28s %6s %6s %7s %7s %7s  %7s %8s" % (
        "map", "blocks", "free", "cells", "ground%", "free%", "gapN", "gap_m"))
    print("-" * 100)
    for r in rows:
        g = max(1, r["grounded"])
        print("%-28s %6d %6d %7d %6.1f%% %6.1f%%  %7d %8.1f" % (
            r["uid"][:28], r["n_grid"], r["n_free"], r["cells"],
            100 * r["either"] / g, 100 * r["free"] / g,
            r["gap_samples"], r["gap_m"]))

    gr = max(1, tot["grounded"])
    print("-" * 100)
    print("samples %d  |  grounded %d  |  airborne %d (%.1f%%)"
          % (tot["samples"], tot["grounded"], tot["airborne"],
             100 * tot["airborne"] / max(1, tot["samples"])))
    print("GROUNDED COVERAGE  %.1f%%   (grid %.1f%%, free-only %.1f%%)"
          % (100 * tot["either"] / gr, 100 * tot["grid"] / gr,
             100 * tot["free"] / gr))
    print("CHECKPOINT-REGION COVERAGE  %.1f%%  (%d/%d grounded samples "
          "within +/-10 of a checkpoint)"
          % (100 * tot["cp_covered"] / max(1, tot["cp_samples"]),
             tot["cp_covered"], tot["cp_samples"]))
    print("airborne samples that also fall inside a block volume: %d (%.1f%%)"
          % (tot["air_covered"],
             100 * tot["air_covered"] / max(1, tot["airborne"])))
    if unknown:
        print("blocks absent from catalogue: %d  %s"
              % (len(unknown), sorted(unknown)[:5]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
