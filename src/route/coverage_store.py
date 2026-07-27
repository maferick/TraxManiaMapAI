"""Compute and persist per-capture telemetry coverage.

Writes ``replay_telemetry_coverage`` and ``replay_terrain_cells``.

Continuous metrics only. No eligibility verdict is computed or stored:
thresholds have to come from the distribution across the captured
cohort, and a 17-map pilot cannot fix one. ``classification`` records
WHY a capture looks poor, with a confidence, so a gate can be derived
later without re-running any matching.

Every input that can move a number is part of the row key, so a re-run
under a new matcher or after item ingestion lands adds a row instead of
overwriting the comparison.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.route.block_matcher import (
    CandidateIndex,
    GROUND_ABS_Y_M,
    GROUND_ROW,
    Placement,
    classify_airborne,
    to_cell,
)

_LOG = logging.getLogger(__name__)

MATCHER_VERSION = "block_matcher-0.2"
AIRBORNE_METHOD = "ballistic_vertical_accel"
AIRBORNE_METHOD_VERSION = "g=-24.0,tol=8.0"

# Vertical slack when testing a footprint cell, matching the coverage
# tool: the measured offset histogram peaks at 0 with a -1 tail.
ROW_TOLERANCE = (0, -1)

# A sample counts as TERRAIN_GROUND when it is unmatched, not airborne,
# and sitting on the ground plane. Every map carries exactly 2,304
# `Grass` baked blocks covering the full 48x48 ground, so "unmatched at
# ground level" is the car driving off the built track rather than a
# hole in the data. Measured: two of three low-coverage pilot maps had
# 99-100% of their unmatched grounded samples here, at y = 8.0 exactly.
TERRAIN_ABS_Y_TOLERANCE_M = 1.5


def parameters_hash(params: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(params, sort_keys=True).encode()
    ).hexdigest()


@dataclass
class CoverageResult:
    metrics: dict[str, Any]
    terrain_cells: dict[tuple[int, int, int], dict[str, Any]]


def compute_coverage(
    samples: Sequence[Mapping[str, float]],
    checkpoint_indices: Sequence[int],
    placements: Sequence[Placement],
    item_cells: Mapping[tuple[int, int, int], int],
    catalogue,
    *,
    free_anchor: str = "center",
    free_pad_m: float = 8.0,
    checkpoint_window: int = 10,
) -> CoverageResult:
    index = CandidateIndex(
        placements, catalogue, free_anchor=free_anchor, free_pad_m=free_pad_m
    )
    air = classify_airborne(samples)
    n = len(samples)

    covered = [False] * n
    src_grid = src_free = src_item = 0
    grounded = airborne = 0
    terrain_samples = 0
    terrain: dict[tuple[int, int, int], dict[str, Any]] = {}

    unmatched_n = 0
    unmatched_m = 0.0
    unmatched_ms = 0
    terrain_m = 0.0
    terrain_ms = 0

    for i, s in enumerate(samples):
        x, y, z = s["x"], s["y"], s["z"]
        step_m = 0.0
        step_ms = 0
        if i > 0:
            p = samples[i - 1]
            step_m = math.dist((x, y, z), (p["x"], p["y"], p["z"]))
            step_ms = int(s["time_ms"]) - int(p["time_ms"])

        if air[i]:
            airborne += 1
            continue
        grounded += 1

        g = any(index.grid_hit(x, y, z, dy=d) for d in ROW_TOLERANCE)
        f = index.free_hit(x, y, z) if not g else False
        cell = to_cell(x, y, z)
        it = (not g and not f) and cell in item_cells
        covered[i] = g or f or it
        src_grid += g
        src_free += f
        src_item += it

        if covered[i]:
            continue

        # Unmatched. Split off the terrain case: on the ground plane it
        # is off-road driving, which is an observation to model rather
        # than missing data.
        on_ground = (
            cell[1] == GROUND_ROW
            and abs(y - GROUND_ABS_Y_M) <= TERRAIN_ABS_Y_TOLERANCE_M
        )
        if on_ground:
            terrain_samples += 1
            terrain_m += step_m
            terrain_ms += max(0, step_ms)
            rec = terrain.setdefault(cell, {
                "first_sample_index": i, "sample_count": 0,
                "duration_ms": 0, "distance_m": 0.0, "hx": 0.0, "hz": 0.0,
            })
            rec["sample_count"] += 1
            rec["duration_ms"] += max(0, step_ms)
            rec["distance_m"] += step_m
            # Heading accumulated as a unit vector so values either side
            # of the +/-pi wrap do not cancel when averaged.
            vx, vz = s.get("vx", 0.0), s.get("vz", 0.0)
            mag = math.hypot(vx, vz)
            if mag > 1e-6:
                rec["hx"] += vx / mag
                rec["hz"] += vz / mag
        else:
            unmatched_n += 1
            unmatched_m += step_m
            unmatched_ms += max(0, step_ms)

    # Longest consecutive unmatched grounded run, excluding terrain:
    # terrain is explained, so folding it in would inflate the gap.
    best_n = best_m = best_ms = 0
    cur_n = 0
    cur_m = 0.0
    cur_ms = 0
    for i, s in enumerate(samples):
        cell = to_cell(s["x"], s["y"], s["z"])
        on_ground = (
            cell[1] == GROUND_ROW
            and abs(s["y"] - GROUND_ABS_Y_M) <= TERRAIN_ABS_Y_TOLERANCE_M
        )
        bad = (not air[i]) and (not covered[i]) and not on_ground
        if bad:
            cur_n += 1
            if i > 0:
                p = samples[i - 1]
                cur_m += math.dist(
                    (s["x"], s["y"], s["z"]), (p["x"], p["y"], p["z"])
                )
                cur_ms += max(0, int(s["time_ms"]) - int(p["time_ms"]))
            if cur_n > best_n:
                best_n, best_m, best_ms = cur_n, cur_m, cur_ms
        else:
            cur_n, cur_m, cur_ms = 0, 0.0, 0

    cp_tot = cp_cov = 0
    for ci in checkpoint_indices:
        for j in range(max(0, ci - checkpoint_window),
                       min(n, ci + checkpoint_window + 1)):
            if air[j]:
                continue
            cp_tot += 1
            cp_cov += covered[j]

    covered_any = sum(1 for i in range(n) if covered[i] and not air[i])
    ground_pct = covered_any / grounded if grounded else 0.0
    terrain_pct = terrain_samples / grounded if grounded else 0.0

    # Reason, with a confidence, not a verdict. Free-form on purpose:
    # the reasons are still being discovered.
    if ground_pct >= 0.8:
        classification, confidence = "block_covered", ground_pct
        reason = "majority of grounded samples resolve to block candidates"
    elif terrain_pct >= 0.5:
        classification, confidence = "terrain_offroad", terrain_pct
        reason = (
            f"{100*terrain_pct:.0f}% of grounded samples are on the ground "
            "plane off the built track"
        )
    else:
        classification = "unresolved_elevated"
        confidence = 1.0 - ground_pct - terrain_pct
        reason = (
            "unmatched samples are elevated, not on terrain; candidate "
            "geometry may be missing (item geometry is not modelled)"
        )

    for rec in terrain.values():
        rec["mean_heading_rad"] = (
            math.atan2(rec["hz"], rec["hx"])
            if (rec["hx"] or rec["hz"]) else None
        )
        rec.pop("hx"), rec.pop("hz")

    return CoverageResult(
        metrics={
            "samples_total": n,
            "samples_airborne": airborne,
            "samples_grounded": grounded,
            "covered_grid": src_grid,
            "covered_free": src_free,
            "covered_item": src_item,
            "covered_any": covered_any,
            "checkpoint_samples": cp_tot,
            "checkpoint_covered": cp_cov,
            "unmatched_samples": unmatched_n,
            "unmatched_distance_m": unmatched_m,
            "unmatched_duration_ms": unmatched_ms,
            "longest_gap_samples": best_n,
            "longest_gap_m": best_m,
            "longest_gap_ms": best_ms,
            "terrain_ground_samples": terrain_samples,
            "terrain_ground_distance_m": terrain_m,
            "terrain_ground_duration_ms": terrain_ms,
            "classification": classification,
            "classification_confidence": round(max(0.0, min(1.0, confidence)), 4),
            "classification_reason": reason,
        },
        terrain_cells=terrain,
    )


def persist(
    conn,
    replay_id: int,
    result: CoverageResult,
    *,
    telemetry_hash: str,
    params: Mapping[str, Any],
    item_ingestion_version: str,
    version: str,
) -> None:
    m = result.metrics
    cols = list(m.keys())
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO replay_telemetry_coverage (
                replay_id, matcher_version, matcher_parameters_hash,
                telemetry_hash, item_ingestion_version,
                airborne_method, airborne_method_version,
                {", ".join(cols)}, created_by_version
            ) VALUES ({", ".join(["%s"] * (8 + len(cols)))})
            ON DUPLICATE KEY UPDATE
                {", ".join(f"{c} = VALUES({c})" for c in cols)}
            """,
            [replay_id, MATCHER_VERSION, parameters_hash(params),
             telemetry_hash, item_ingestion_version,
             AIRBORNE_METHOD, AIRBORNE_METHOD_VERSION]
            + [m[c] for c in cols] + [version],
        )

        cur.execute(
            "DELETE FROM replay_terrain_cells WHERE replay_id=%s "
            "AND matcher_version=%s",
            (replay_id, MATCHER_VERSION),
        )
        rows = [
            (replay_id, MATCHER_VERSION, c[0], c[1], c[2],
             r["first_sample_index"], r["sample_count"], r["duration_ms"],
             r["distance_m"], r["mean_heading_rad"], version)
            for c, r in result.terrain_cells.items()
        ]
        if rows:
            cur.executemany(
                """
                INSERT INTO replay_terrain_cells (
                    replay_id, matcher_version, cell_x, cell_y, cell_z,
                    first_sample_index, sample_count, duration_ms,
                    distance_m, mean_heading_rad, created_by_version
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                rows,
            )
    conn.commit()


def load_placements(conn, map_id: int) -> list[Placement]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, block_type, variant, is_free, x, y, z, rotation,
                   abs_x, abs_y, abs_z, yaw, pitch, roll
              FROM block_placements WHERE map_id = %s
            """,
            (map_id,),
        )
        out = []
        for r in cur.fetchall():
            try:
                variant = int(r[2]) if r[2] not in (None, "") else 0
            except (TypeError, ValueError):
                variant = 0
            out.append(Placement(
                index=r[0], block_type=r[1], variant=variant,
                is_free=bool(r[3]),
                x=r[4], y=r[5], z=r[6], rotation=r[7] or 0,
                abs_x=float(r[8]) if r[8] is not None else None,
                abs_y=float(r[9]) if r[9] is not None else None,
                abs_z=float(r[10]) if r[10] is not None else None,
                yaw=float(r[11] or 0.0), pitch=float(r[12] or 0.0),
                roll=float(r[13] or 0.0),
            ))
    return out


def load_item_cells(conn, map_id: int, radius: int = 1) -> dict:
    """Candidate cells from item anchors, expanded by a cell radius.

    Radius stands in for the footprint we do not have. Item geometry is
    unavailable offline, so an item that spans several cells is known
    only at its anchor. This is candidate generation, so widening is the
    safe error.
    """
    cells: dict[tuple[int, int, int], int] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, cell_x, cell_y, cell_z FROM map_items "
            "WHERE map_id=%s AND cell_x IS NOT NULL",
            (map_id,),
        )
        for item_id, cx, cy, cz in cur.fetchall():
            for dx in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    for dy in (0, -1):
                        cells.setdefault((cx + dx, cy + dy, cz + dz), item_id)
    return cells
