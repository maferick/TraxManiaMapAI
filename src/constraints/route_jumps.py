"""Learn jumps from the fact that every published map is finishable.

This is the repo owner's observation, and it is what makes jumps
learnable at all: every map in the corpus was published and parses
cleanly, so every one of them can be driven start to finish. Therefore
a gap the racing line **must** cross IS drivable — and we never have to
reason about physics, launch angles or speed. Only about which gaps the
line has no choice but to cross.

The definition that failed was proximity. At radius 3 the placement
grammar found that 81% of surviving rows were two unrelated blocks that
happened to sit a few cells apart, indistinguishable from a take-off
and a landing, which is why jumps had to be switched off by default.

The definition that works is **open end to open end**:

* a route block face with no neighbour is a place the racing line stops
* if another such open face points back at it across empty cells, and
  the map is finishable, then the car crossed that gap through the air

Nothing else can explain it. There is no block in between, and the map
demonstrably completes.
"""
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field

import pymysql
from pymysql.connections import Connection

from src.catalogue.loader import BlockDef, FACE_DELTAS, rotate_offset, rotate_vector
from src.constraints.route_sequences import (
    _IS_SUPPORT,
    _MAPS_SQL,
    _PLACEMENTS_SQL,
)
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)

STAGE_VERSION = "route-jumps-v1"

# Longest gap treated as a jump. Beyond this the "open end facing an
# open end" coincidence rate climbs and a real map would use a
# structure, not air.
MAX_JUMP_GAP = 6


@dataclass
class JumpReport:
    maps_seen: int = 0
    maps_with_jumps: int = 0
    open_ends: int = 0
    jumps: int = 0
    rows_written: int = 0
    errors: list[str] = field(default_factory=list)


_UPSERT = """
INSERT INTO block_jump_pairs (
    jump_signature, block_a, block_b, dx, dy, dz, rel_rotation, gap,
    environment, occurrences, map_count, created_by_version
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE
    occurrences = occurrences + VALUES(occurrences),
    map_count   = map_count   + VALUES(map_count),
    updated_at  = CURRENT_TIMESTAMP(6),
    created_by_version = VALUES(created_by_version)
"""


def reset_jumps(conn: Connection) -> None:
    with cursor(conn) as cur:
        cur.execute("TRUNCATE TABLE block_jump_pairs")
    conn.commit()


def extract_jumps(
    rows: list[tuple], catalogue: dict[str, BlockDef],
) -> tuple[list[tuple], int]:
    """Open-end-to-open-end gaps in one map. Returns (jumps, open_ends)."""
    placements = [
        (str(r[0]), int(r[1]) % 4, int(r[2]), int(r[3]), int(r[4]))
        for r in rows if not _IS_SUPPORT(str(r[0]))
    ]
    owner: dict[tuple[int, int, int], int] = {}
    cells_of: dict[int, list[tuple[int, int, int]]] = {}
    for i, (name, rot, x, y, z) in enumerate(placements):
        block = catalogue.get(name)
        variant = block.variant("ground", 0) if block is not None else None
        cells = (
            [rotate_offset(u.offset, rot, variant.size) for u in variant.units]
            if variant is not None else [(0, 0, 0)]
        )
        world = [(x + c[0], y + c[1], z + c[2]) for c in cells]
        cells_of[i] = world
        for w in world:
            owner[w] = i

    open_ends: list[tuple[int, tuple[int, int, int], tuple[int, int, int]]] = []
    for i, world in cells_of.items():
        own = set(world)
        for cell in world:
            for d in FACE_DELTAS.values():
                probe = (cell[0] + d[0], cell[1] + d[1], cell[2] + d[2])
                if probe in own or probe in owner:
                    continue
                open_ends.append((i, cell, d))

    jumps: list[tuple] = []
    for i, cell, d in open_ends:
        for gap in range(1, MAX_JUMP_GAP + 1):
            probe = (
                cell[0] + d[0] * (gap + 1),
                cell[1] + d[1] * (gap + 1),
                cell[2] + d[2] * (gap + 1),
            )
            j = owner.get(probe)
            if j is None or j == i:
                continue
            # The landing must ALSO be an open end pointing back. Without
            # this it is merely a block downrange, which is the
            # coincidence that made proximity useless.
            back = (-d[0], -d[1], -d[2])
            facing = (
                probe[0] + back[0], probe[1] + back[1], probe[2] + back[2],
            )
            if facing in owner:
                break
            a, b = placements[i], placements[j]
            world_step = (b[2] - a[2], b[3] - a[3], b[4] - a[4])
            dx, dy, dz = rotate_vector(world_step, (4 - a[1]) % 4)
            jumps.append((a[0], b[0], dx, dy, dz, (b[1] - a[1]) % 4, gap))
            break
    return jumps, len(open_ends)


def build_jumps(
    conn: Connection,
    catalogue: dict[str, BlockDef],
    environment: str = "Stadium2020",
    limit: int | None = None,
    min_map_count: int = 3,
) -> JumpReport:
    report = JumpReport()
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    with cursor(conn) as cur:
        cur.execute(_MAPS_SQL.format(limit_clause=limit_clause), (environment,))
        map_ids = [int(r[0]) for r in cur.fetchall()]
    _LOG.info("%s: %d maps in %s", STAGE_VERSION, len(map_ids), environment)

    counts: dict[tuple, int] = defaultdict(int)
    maps_with: dict[tuple, int] = defaultdict(int)
    for map_id in map_ids:
        report.maps_seen += 1
        if report.maps_seen % 1000 == 0:
            _LOG.info(
                "%s: %d/%d maps, %d jumps, %d keys",
                STAGE_VERSION, report.maps_seen, len(map_ids),
                report.jumps, len(counts),
            )
        try:
            with cursor(conn) as cur:
                cur.execute(_PLACEMENTS_SQL, (map_id,))
                rows = cur.fetchall()
        except pymysql.MySQLError as exc:  # pragma: no cover - transient
            report.errors.append(f"map {map_id}: {exc}")
            continue
        if not rows:
            continue
        jumps, opens = extract_jumps(rows, catalogue)
        report.open_ends += opens
        report.jumps += len(jumps)
        if jumps:
            report.maps_with_jumps += 1
        here = set(jumps)
        for key in jumps:
            counts[key] += 1
        for key in here:
            maps_with[key] += 1

    _LOG.info(
        "%s: %d/%d maps have jumps (%.1f%%), %d open ends, %d jump "
        "observations, %d distinct keys",
        STAGE_VERSION, report.maps_with_jumps, report.maps_seen,
        100.0 * report.maps_with_jumps / max(1, report.maps_seen),
        report.open_ends, report.jumps, len(counts),
    )

    batch: list[tuple] = []
    with cursor(conn) as cur:
        for key, count in counts.items():
            if maps_with[key] < min_map_count:
                continue
            a, b, dx, dy, dz, rel, gap = key
            payload = "|".join(str(v) for v in key) + f"|{environment}"
            batch.append((
                hashlib.sha256(payload.encode()).hexdigest(),
                a, b, dx, dy, dz, rel, gap, environment,
                count, maps_with[key], STAGE_VERSION,
            ))
            report.rows_written += 1
            if len(batch) >= 5000:
                cur.executemany(_UPSERT, batch)
                conn.commit()
                batch.clear()
        if batch:
            cur.executemany(_UPSERT, batch)
    conn.commit()
    _LOG.info("%s: %d rows written", STAGE_VERSION, report.rows_written)
    return report
