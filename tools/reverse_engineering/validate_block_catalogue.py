"""Sanity-check a BlockCatalogueDump NDJSON catalogue.

Usage:
    python tools/reverse_engineering/validate_block_catalogue.py <catalogue.ndjson>

Read-only. Prints a report and exits non-zero if any hard check fails.
Hard checks are deliberately conservative: they verify the dump is
complete and structurally usable, not that every block is perfect.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

SCHEMA = "block_catalogue_v1"

# Blocks any Stadium catalogue must contain. If these are missing the
# dump did not actually walk the block inventory.
EXPECTED_IDS = [
    "RoadTechStraight",
    "RoadTechCurve1",
    "RoadTechCheckpoint",
    "RoadTechStart",
    "RoadTechFinish",
    "PlatformTechBase",
]

FACES = ("n", "e", "s", "w", "top", "bottom")


def fail(msg: str) -> None:
    print(f"FAIL  {msg}")
    fail.count += 1  # type: ignore[attr-defined]


fail.count = 0  # type: ignore[attr-defined]


def main(path_arg: str) -> int:
    path = Path(path_arg)
    if not path.is_file():
        print(f"FAIL  catalogue not found: {path}")
        return 1

    done_path = path.with_name("catalogue.done.json")
    done = None
    if done_path.is_file():
        done = json.loads(done_path.read_text(encoding="utf-8"))
    else:
        fail(f"no completion marker next to catalogue: {done_path.name}")

    blocks: dict[str, dict] = {}
    meta = None
    bad_lines = 0
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if lineno == 1 and rec.get("type") == "meta":
                meta = rec
                continue
            if rec.get("type") == "block":
                blocks[rec["id"]] = rec

    print(f"catalogue: {path}")
    print(f"blocks: {len(blocks)}, malformed lines: {bad_lines}")

    if meta is None or meta.get("schema") != SCHEMA:
        fail(f"missing/wrong meta line (want schema {SCHEMA!r})")
    if bad_lines:
        fail(f"{bad_lines} malformed JSON lines")
    if done is not None and done.get("blocks_dumped") != len(blocks):
        fail(
            f"done marker says {done.get('blocks_dumped')} blocks, "
            f"file has {len(blocks)}"
        )
    if len(blocks) < 1000:
        fail(f"only {len(blocks)} blocks; a full Stadium inventory is >1000")

    for block_id in EXPECTED_IDS:
        if block_id not in blocks:
            fail(f"expected block missing: {block_id}")

    # --- soft stats -------------------------------------------------
    # Runtime dumps carry the raw EWayPointType int; offline (GBX.NET)
    # dumps carry the enum name. Normalise to names for the gate.
    wp_names = {0: "Start", 1: "Finish", 2: "Checkpoint", 3: "None",
                4: "StartFinish", 5: "Dispenser"}

    n_variants = 0
    n_units = 0
    with_clips = 0
    multi_cell = 0
    waypoints = Counter()
    clip_ids = Counter()
    for rec in blocks.values():
        wp = rec.get("waypoint")
        waypoints[wp_names.get(wp, wp)] += 1
        block_has_clip = False
        for var in rec.get("variants", []):
            n_variants += 1
            size = var.get("size", [1, 1, 1])
            if any(int(d) > 1 for d in size):
                multi_cell += 1
            for unit in var.get("units", []):
                n_units += 1
                for face in FACES:
                    for cid in unit.get("clips", {}).get(face, []):
                        clip_ids[cid] += 1
                        block_has_clip = True
        if block_has_clip:
            with_clips += 1

    print(f"variants: {n_variants}, units: {n_units}")
    print(f"blocks with >=1 clip: {with_clips} "
          f"({100 * with_clips // max(len(blocks), 1)}%)")
    print(f"multi-cell variants: {multi_cell}")
    print(f"waypoint mix: {dict(sorted(waypoints.items()))}")
    print(f"distinct clip ids: {len(clip_ids)}; top 10: "
          f"{clip_ids.most_common(10)}")

    if with_clips == 0:
        fail("no block carries any clip — clip buffers were not read")
    if multi_cell == 0:
        fail("no multi-cell variant found — Size was not read correctly")
    for label in ("Start", "Finish", "Checkpoint"):
        if waypoints.get(label, 0) == 0:
            fail(f"no {label} blocks in catalogue")

    if fail.count:  # type: ignore[attr-defined]
        print(f"\n{fail.count} hard check(s) failed")  # type: ignore[attr-defined]
        return 1
    print("\nOK — catalogue is structurally usable")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
