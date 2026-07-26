"""Reconstruct each corpus map's racing line, then mine ordered sequences.

Everything built so far is pairwise CO-OCCURRENCE, and that has two
limits no amount of tuning fixes:

* **It is direction-blind.** A symmetric straight records the same
  physical scene under both rotation 0 and rotation 2, so "what follows
  what" splits evenly no matter what. Validating the booster rule
  needed an asymmetric block precisely because of this.
* **It cannot express a sequence.** Mappers do not choose blocks
  pairwise; a chicane into a straight into a booster is a pattern. A
  bigram model can only ever reproduce the marginal.

Both dissolve if the route ORDER is known. So recover it: a map's
racing line is the clip-matched chain from its Start to its Finish, and
clip matching is the game's own join relation for road surfaces (see
``src/catalogue/loader.py``). Walk that chain and the result is an
ordered block sequence per map, from which directional pairs and
triples fall out directly.

What this deliberately does NOT claim: that every map is a clip chain.
Platform maps are not — their gates carry an isolated clip — and the
stage reports how many maps it could and could not reconstruct rather
than quietly averaging over the ones that worked.
"""
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field

import pymysql
from pymysql.connections import Connection

from src.catalogue.loader import (
    BlockDef,
    FACE_DELTAS,
    opposite_face,
    rotate_face,
    rotate_offset,
    rotate_vector,
)
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)

STAGE_VERSION = "route-sequences-v1"


@dataclass
class SequenceReport:
    maps_seen: int = 0
    no_start: int = 0
    no_finish: int = 0
    no_path: int = 0
    reconstructed: int = 0
    route_blocks: int = 0
    pairs: int = 0
    triples: int = 0
    rows_written: int = 0
    errors: list[str] = field(default_factory=list)


_MAPS_SQL = """
SELECT id FROM maps
WHERE parse_status = 'success' AND environment = %s
ORDER BY id
{limit_clause}
"""

_PLACEMENTS_SQL = """
SELECT block_type, rotation, x, y, z
FROM block_placements
WHERE map_id = %s AND is_free = 0
  AND x IS NOT NULL AND y IS NOT NULL AND z IS NOT NULL
"""

_UPSERT_SQL = """
INSERT INTO block_route_sequences (
    seq_signature, n, block_a, block_b, block_c,
    dx1, dy1, dz1, rel1, dx2, dy2, dz2, rel2,
    environment, occurrences, map_count, created_by_version
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE
    occurrences = occurrences + VALUES(occurrences),
    map_count   = map_count   + VALUES(map_count),
    updated_at  = CURRENT_TIMESTAMP(6),
    created_by_version = VALUES(created_by_version)
"""


def reset_sequences(conn: Connection) -> None:
    with cursor(conn) as cur:
        cur.execute("TRUNCATE TABLE block_route_sequences")
    conn.commit()


def _port_index(
    catalogue: dict[str, BlockDef],
) -> dict[tuple[str, int], list[tuple[tuple[int, int, int], int, str]]]:
    index: dict[tuple[str, int], list] = {}
    for block_id, block in catalogue.items():
        variant = block.variant("ground", 0)
        if variant is None:
            continue
        local = variant.side_ports()
        if not local:
            continue
        for rotation in range(4):
            index[(block_id, rotation)] = [
                (
                    rotate_offset(p.offset, rotation, variant.size),
                    rotate_face(p.face, rotation),
                    p.clip_id,
                )
                for p in local
            ]
    return index


def reconstruct_route(
    rows: list[tuple],
    catalogue: dict[str, BlockDef],
    ports: dict,
) -> list[tuple] | None:
    """The clip-matched chain from Start to Finish, in driving order.

    Returns ``None`` when the map has no Start, no Finish, or no chain
    joining them — all three are real and are counted, not hidden.
    Platform maps land here because their gates use an isolated clip.
    """
    placements = [
        (str(r[0]), int(r[1]) % 4, int(r[2]), int(r[3]), int(r[4]))
        for r in rows
    ]
    # World port cell+face -> (placement index, clip)
    open_ports: dict[tuple[int, int, int, int], list[tuple[int, str]]] = (
        defaultdict(list)
    )
    for i, (name, rot, x, y, z) in enumerate(placements):
        for cell, face, clip in ports.get((name, rot), ()):
            open_ports[(x + cell[0], y + cell[1], z + cell[2], face)].append(
                (i, clip)
            )

    links: dict[int, set[int]] = defaultdict(set)
    for (wx, wy, wz, face), here in open_ports.items():
        dx, dy, dz = FACE_DELTAS[face]
        there = open_ports.get((wx + dx, wy + dy, wz + dz, opposite_face(face)))
        if not there:
            continue
        for i, clip_i in here:
            for j, clip_j in there:
                if i != j and clip_i == clip_j:
                    links[i].add(j)
                    links[j].add(i)

    starts = [
        i for i, p in enumerate(placements)
        if catalogue.get(p[0]) is not None
        and catalogue[p[0]].waypoint in ("Start", "StartFinish")
    ]
    finishes = [
        i for i, p in enumerate(placements)
        if catalogue.get(p[0]) is not None
        and catalogue[p[0]].waypoint in ("Finish", "StartFinish")
    ]
    if not starts:
        return None
    if not finishes:
        return None

    goal = set(finishes)
    # Breadth-first: the racing line is the shortest clip-matched chain
    # between the two waypoints. A longest path would be truer to how a
    # track is driven but is NP-hard, and the shortest chain is enough
    # to fix ORDER, which is the whole point here.
    for start in starts:
        prev: dict[int, int] = {start: -1}
        queue = deque([start])
        hit = None
        while queue:
            cur = queue.popleft()
            if cur in goal and cur != start:
                hit = cur
                break
            for nxt in links[cur]:
                if nxt not in prev:
                    prev[nxt] = cur
                    queue.append(nxt)
        if hit is None:
            continue
        chain = []
        node = hit
        while node != -1:
            chain.append(placements[node])
            node = prev[node]
        chain.reverse()
        if len(chain) >= 3:
            return chain
    return None


def _step(a: tuple, b: tuple) -> tuple[int, int, int, int]:
    """B's offset in A's frame plus their relative rotation — directional."""
    world = (b[2] - a[2], b[3] - a[3], b[4] - a[4])
    dx, dy, dz = rotate_vector(world, (4 - a[1]) % 4)
    return dx, dy, dz, (b[1] - a[1]) % 4


def build_sequences(
    conn: Connection,
    catalogue: dict[str, BlockDef],
    environment: str = "Stadium2020",
    limit: int | None = None,
    min_map_count: int = 3,
) -> SequenceReport:
    report = SequenceReport()
    ports = _port_index(catalogue)

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
                "%s: %d/%d maps, %d reconstructed, %d keys",
                STAGE_VERSION, report.maps_seen, len(map_ids),
                report.reconstructed, len(counts),
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

        chain = reconstruct_route(rows, catalogue, ports)
        if chain is None:
            report.no_path += 1
            continue
        report.reconstructed += 1
        report.route_blocks += len(chain)

        here: set[tuple] = set()
        for a, b in zip(chain, chain[1:]):
            dx, dy, dz, rel = _step(a, b)
            key = (2, a[0], b[0], "", dx, dy, dz, rel, 0, 0, 0, 0)
            counts[key] += 1
            report.pairs += 1
            here.add(key)
        for a, b, c in zip(chain, chain[1:], chain[2:]):
            dx1, dy1, dz1, rel1 = _step(a, b)
            dx2, dy2, dz2, rel2 = _step(b, c)
            key = (3, a[0], b[0], c[0], dx1, dy1, dz1, rel1,
                   dx2, dy2, dz2, rel2)
            counts[key] += 1
            report.triples += 1
            here.add(key)
        for key in here:
            maps_with[key] += 1

    _LOG.info(
        "%s: reconstructed %d/%d maps (%.1f%%), %d route blocks, "
        "%d pair + %d triple observations, %d distinct keys",
        STAGE_VERSION, report.reconstructed, report.maps_seen,
        100.0 * report.reconstructed / max(1, report.maps_seen),
        report.route_blocks, report.pairs, report.triples, len(counts),
    )

    batch: list[tuple] = []
    with cursor(conn) as cur:
        for key, count in counts.items():
            if maps_with[key] < min_map_count:
                continue
            n, a, b, c, dx1, dy1, dz1, rel1, dx2, dy2, dz2, rel2 = key
            payload = "|".join(str(v) for v in key) + f"|{environment}"
            batch.append((
                hashlib.sha256(payload.encode()).hexdigest(),
                n, a, b, c, dx1, dy1, dz1, rel1, dx2, dy2, dz2, rel2,
                environment, count, maps_with[key], STAGE_VERSION,
            ))
            report.rows_written += 1
            if len(batch) >= 5000:
                cur.executemany(_UPSERT_SQL, batch)
                conn.commit()
                batch.clear()
        if batch:
            cur.executemany(_UPSERT_SQL, batch)
    conn.commit()
    _LOG.info("%s: %d rows written", STAGE_VERSION, report.rows_written)
    return report
