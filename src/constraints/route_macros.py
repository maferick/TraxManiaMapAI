"""Recurring multi-block runs — the units a planner composes with.

The ordered triples were mined and then measured as a per-step weight,
where they made maps worse at every setting: the strongest triples are
same-block runs, so rewarding them rewards repetition. That was the
wrong granularity, not the wrong data.

A macro is the right granularity. Instead of asking "what block comes
next", ask "what four-to-eight block RUN do mappers build", and place
the whole run. `SpecialTurbo2 x3` stops being a weight nudge and
becomes a booster section; `SlopeStraight x3` becomes a climb.

Extraction reuses the opposing-face idea that made triples work. A
block whose neighbours sit on opposing faces is a through-piece; chain
through-pieces and the maximal chain is a run of the racing line. No
route order is needed, so there is nothing to short-circuit — the
Start-to-Finish shortest path that failed twice is not involved.

Runs are stored canonically (a run and its reverse are one macro,
since the extraction is direction-ambiguous) with each step's offset
in the previous block's frame, so a macro can be applied at any
heading by the same arithmetic a single move uses.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field

import pymysql
from pymysql.connections import Connection

from src.catalogue.loader import BlockDef, FACE_DELTAS, rotate_offset, rotate_vector
from src.constraints.route_jumps import _IS_ROUTE_SURFACE
from src.constraints.route_sequences import _IS_SUPPORT, _MAPS_SQL, _PLACEMENTS_SQL
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)

STAGE_VERSION = "route-macros-v1"

MIN_RUN = 4
MAX_RUN = 8


def _sign(v: int) -> int:
    return (v > 0) - (v < 0)


@dataclass
class MacroReport:
    maps_seen: int = 0
    maps_with_runs: int = 0
    runs: int = 0
    rows_written: int = 0
    errors: list[str] = field(default_factory=list)


_UPSERT = """
INSERT INTO block_macros (
    macro_signature, length, blocks_json, steps_json, environment,
    occurrences, map_count, created_by_version
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE
    occurrences = occurrences + VALUES(occurrences),
    map_count   = map_count   + VALUES(map_count),
    updated_at  = CURRENT_TIMESTAMP(6),
    created_by_version = VALUES(created_by_version)
"""


def reset_macros(conn: Connection) -> None:
    with cursor(conn) as cur:
        cur.execute("TRUNCATE TABLE block_macros")
    conn.commit()


def extract_runs(
    rows: list[tuple],
    catalogue: dict[str, BlockDef],
    min_run: int = MIN_RUN,
    max_run: int = MAX_RUN,
) -> list[list[tuple]]:
    """Maximal chains of through-pieces, split into runs."""
    placements = [
        (str(r[0]), int(r[1]) % 4, int(r[2]), int(r[3]), int(r[4]))
        for r in rows
        if not _IS_SUPPORT(str(r[0])) and _IS_ROUTE_SURFACE(str(r[0]))
    ]
    if len(placements) < min_run:
        return []

    owner: dict[tuple[int, int, int], int] = {}
    cells_of: dict[int, list[tuple[int, int, int]]] = {}
    for i, (name, rot, x, y, z) in enumerate(placements):
        block = catalogue.get(name)
        variant = block.variant("ground", 0) if block is not None else None
        cells = (
            [rotate_offset(u.offset, rot, variant.size) for u in variant.units]
            if variant is not None and variant.units else [(0, 0, 0)]
        )
        world = [(x + c[0], y + c[1], z + c[2]) for c in cells]
        cells_of[i] = world
        for w in world:
            owner[w] = i

    deltas = (*FACE_DELTAS.values(), (0, 1, 0), (0, -1, 0))
    links: dict[int, set[int]] = defaultdict(set)
    for i, world in cells_of.items():
        for cell in world:
            for d in deltas:
                j = owner.get((cell[0] + d[0], cell[1] + d[1], cell[2] + d[2]))
                if j is not None and j != i:
                    links[i].add(j)
                    links[j].add(i)

    # A through-piece has exactly two neighbours on opposing sides.
    forward: dict[int, tuple[int, int]] = {}
    for i, p_i in enumerate(placements):
        by_dir: dict[tuple[int, int, int], int] = {}
        for j in links.get(i, ()):
            p_j = placements[j]
            step = (
                _sign(p_j[2] - p_i[2]),
                _sign(p_j[3] - p_i[3]),
                _sign(p_j[4] - p_i[4]),
            )
            by_dir.setdefault(step, j)
        pairs = [
            (a, b) for step, a in by_dir.items()
            if (b := by_dir.get((-step[0], -step[1], -step[2]))) is not None
            and a != b
        ]
        if pairs:
            forward[i] = pairs[0]

    seen: set[int] = set()
    runs: list[list[tuple]] = []
    for start in forward:
        if start in seen:
            continue
        chain = [start]
        seen.add(start)
        # Walk both ways from this through-piece.
        for side in (0, 1):
            cur, prev = forward[start][side], start
            while cur in forward and cur not in seen:
                seen.add(cur)
                if side:
                    chain.append(cur)
                else:
                    chain.insert(0, cur)
                a, b = forward[cur]
                nxt = b if a == prev else a
                cur, prev = nxt, cur
        if len(chain) >= min_run:
            for i in range(0, len(chain) - min_run + 1):
                window = chain[i: i + max_run]
                if len(window) >= min_run:
                    runs.append([placements[k] for k in window])
    return runs


def canonical_macro(run: list[tuple]) -> tuple[str, list[str], list[list[int]]]:
    """Blocks + per-step offsets, with the run and its reverse unified."""
    def encode(seq: list[tuple]) -> tuple[list[str], list[list[int]]]:
        blocks = [p[0] for p in seq]
        steps: list[list[int]] = []
        for a, b in zip(seq, seq[1:]):
            world = (b[2] - a[2], b[3] - a[3], b[4] - a[4])
            dx, dy, dz = rotate_vector(world, (4 - a[1]) % 4)
            steps.append([dx, dy, dz, (b[1] - a[1]) % 4])
        return blocks, steps

    fwd = encode(run)
    rev = encode(list(reversed(run)))
    # Extraction is direction-ambiguous, so pick a stable representative
    # rather than storing both halves of the same physical run.
    chosen = min((fwd, rev), key=lambda e: (e[0], e[1]))
    payload = json.dumps(chosen, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest(), chosen[0], chosen[1]


def build_macros(
    conn: Connection,
    catalogue: dict[str, BlockDef],
    environment: str = "Stadium2020",
    limit: int | None = None,
    min_map_count: int = 3,
) -> MacroReport:
    report = MacroReport()
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    with cursor(conn) as cur:
        cur.execute(_MAPS_SQL.format(limit_clause=limit_clause), (environment,))
        map_ids = [int(r[0]) for r in cur.fetchall()]
    _LOG.info("%s: %d maps in %s", STAGE_VERSION, len(map_ids), environment)

    counts: dict[str, int] = defaultdict(int)
    maps_with: dict[str, int] = defaultdict(int)
    payloads: dict[str, tuple] = {}

    for map_id in map_ids:
        report.maps_seen += 1
        if report.maps_seen % 1000 == 0:
            _LOG.info(
                "%s: %d/%d maps, %d runs, %d macros",
                STAGE_VERSION, report.maps_seen, len(map_ids),
                report.runs, len(counts),
            )
        try:
            with cursor(conn) as cur:
                cur.execute(_PLACEMENTS_SQL, (map_id,))
                rows = cur.fetchall()
        except pymysql.MySQLError as exc:  # pragma: no cover - transient
            report.errors.append(f"map {map_id}: {exc}")
            continue
        runs = extract_runs(rows, catalogue)
        if runs:
            report.maps_with_runs += 1
        report.runs += len(runs)
        here: set[str] = set()
        for run in runs:
            sig, blocks, steps = canonical_macro(run)
            counts[sig] += 1
            payloads[sig] = (len(blocks), blocks, steps)
            here.add(sig)
        for sig in here:
            maps_with[sig] += 1

    _LOG.info(
        "%s: %d/%d maps yielded runs, %d runs, %d distinct macros",
        STAGE_VERSION, report.maps_with_runs, report.maps_seen,
        report.runs, len(counts),
    )

    batch: list[tuple] = []
    with cursor(conn) as cur:
        for sig, count in counts.items():
            if maps_with[sig] < min_map_count:
                continue
            length, blocks, steps = payloads[sig]
            batch.append((
                sig, length,
                json.dumps(blocks, separators=(",", ":")),
                json.dumps(steps, separators=(",", ":")),
                environment, count, maps_with[sig], STAGE_VERSION,
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
