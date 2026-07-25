"""Face-aware block-transition extractor (catalogue era).

Counts clip-matched port meetings between grid placements: block A's
route-clip port looks at a neighbouring cell where block B exposes a
matching port on the facing side. This is the game's own join
relation (see ``src/catalogue/loader.py`` for the calibrated rotation
model), so unlike raw cell adjacency it cannot count decorative
stacks, parallel roads, or a curve's blind back wall as transitions.

Coverage: every parsed map contributes (corpus-finishable axiom —
published + parseable implies finishable), so these priors exist for
the whole corpus, not just the replay-backed slice that feeds
``block_pair_transitions``.

Weighting signal only. Frequency never promotes validity — the
composition rule from the pair-count stage carries over unchanged.
"""
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from pymysql.connections import Connection

from src.catalogue.loader import (
    FACE_DELTAS,
    BlockDef,
    opposite_face,
    rotate_face,
    rotate_offset,
)
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)

STAGE_VERSION = "face-transitions-v0.1"

# Same route-clip scope as the walker: wall/scenery clips join
# scenery, not road. Extend deliberately, family by family.
DEFAULT_ROUTE_CLIPS = frozenset({"RoadTechFC"})


@dataclass(frozen=True)
class _TransitionKey:
    block_a: str
    block_b: str
    clip_id: str
    rel_rotation: int
    environment: str

    def signature(self) -> str:
        payload = "|".join((
            self.block_a, self.block_b, self.clip_id,
            str(self.rel_rotation), self.environment,
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class FaceTransitionReport:
    maps_seen: int = 0
    placements_seen: int = 0
    placements_unknown_block: int = 0
    transitions_counted: int = 0
    rows_written: int = 0
    errors: list[str] = field(default_factory=list)


_SCAN_MAP_IDS_SQL = """
SELECT m.id, COALESCE(m.environment, '') AS environment
FROM maps m
WHERE m.parse_status = 'success'
ORDER BY m.id
{limit_clause}
"""

_PLACEMENTS_SQL = """
SELECT block_type, rotation, x, y, z
FROM block_placements
WHERE map_id = %s AND is_free = 0
  AND x IS NOT NULL AND y IS NOT NULL AND z IS NOT NULL
"""

_UPSERT_SQL = """
INSERT INTO block_face_transitions (
    transition_signature, block_a, block_b, clip_id, rel_rotation,
    environment, transition_count, map_count, created_by_version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    transition_count = transition_count + VALUES(transition_count),
    map_count        = map_count        + VALUES(map_count),
    updated_at       = CURRENT_TIMESTAMP(6),
    created_by_version = VALUES(created_by_version)
"""


def reset_face_transitions(conn: Connection) -> None:
    with cursor(conn) as cur:
        cur.execute("TRUNCATE TABLE block_face_transitions")
    conn.commit()


def _port_index(
    catalogue: dict[str, BlockDef],
    route_clips: frozenset[str],
) -> dict[str, list[tuple[tuple[int, int, int], int, str]]]:
    """(block_id, rotation) -> list of (cell_offset, world_face, clip).

    Flattened to ``block_id -> per-rotation lists`` keyed
    ``f"{block_id}\\x00{rotation}"`` to keep lookups dict-simple.
    """
    index: dict[str, list[tuple[tuple[int, int, int], int, str]]] = {}
    for block_id, block in catalogue.items():
        variant = block.variant("ground", 0)
        if variant is None:
            continue
        local = [p for p in variant.side_ports() if p.clip_id in route_clips]
        if not local:
            continue
        for rotation in range(4):
            ports = [
                (
                    rotate_offset(p.offset, rotation, variant.size),
                    rotate_face(p.face, rotation),
                    p.clip_id,
                )
                for p in local
            ]
            index[f"{block_id}\x00{rotation}"] = ports
    return index


def build_face_transitions(
    conn: Connection,
    catalogue: dict[str, BlockDef],
    limit: int | None = None,
    route_clips: frozenset[str] = DEFAULT_ROUTE_CLIPS,
) -> FaceTransitionReport:
    report = FaceTransitionReport()
    ports_by_block_rot = _port_index(catalogue, route_clips)

    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    with cursor(conn) as cur:
        cur.execute(_SCAN_MAP_IDS_SQL.format(limit_clause=limit_clause))
        maps = [(int(r[0]), str(r[1])) for r in cur.fetchall()]

    counts: dict[_TransitionKey, int] = defaultdict(int)
    map_hits: dict[_TransitionKey, int] = defaultdict(int)

    for map_id, environment in maps:
        report.maps_seen += 1
        with cursor(conn) as cur:
            cur.execute(_PLACEMENTS_SQL, (map_id,))
            rows = cur.fetchall()

        # world port cell + face -> (block_id, rotation, clip)
        open_ports: dict[
            tuple[int, int, int, int], tuple[str, int, str]
        ] = {}
        per_map: set[_TransitionKey] = set()

        for block_type, rotation, x, y, z in rows:
            report.placements_seen += 1
            key = f"{block_type}\x00{int(rotation) % 4}"
            ports = ports_by_block_rot.get(key)
            if ports is None:
                if f"{block_type}\x000" not in ports_by_block_rot:
                    report.placements_unknown_block += 1
                continue
            for cell, face, clip in ports:
                wx, wy, wz = int(x) + cell[0], int(y) + cell[1], int(z) + cell[2]
                open_ports[(wx, wy, wz, face)] = (
                    str(block_type), int(rotation) % 4, clip,
                )

        for (wx, wy, wz, face), (block_a, rot_a, clip) in open_ports.items():
            dx, dy, dz = FACE_DELTAS[face]
            other = open_ports.get(
                (wx + dx, wy + dy, wz + dz, opposite_face(face))
            )
            if other is None or other[2] != clip:
                continue
            block_b, rot_b, _ = other
            tkey = _TransitionKey(
                block_a=block_a,
                block_b=block_b,
                clip_id=clip,
                rel_rotation=(rot_b - rot_a) % 4,
                environment=environment,
            )
            counts[tkey] += 1
            report.transitions_counted += 1
            per_map.add(tkey)

        for tkey in per_map:
            map_hits[tkey] += 1

    with cursor(conn) as cur:
        for tkey, count in counts.items():
            cur.execute(_UPSERT_SQL, (
                tkey.signature(), tkey.block_a, tkey.block_b, tkey.clip_id,
                tkey.rel_rotation, tkey.environment,
                count, map_hits[tkey], STAGE_VERSION,
            ))
            report.rows_written += 1
    conn.commit()

    _LOG.info(
        "face-transitions: maps=%d placements=%d unknown=%d "
        "transitions=%d rows=%d",
        report.maps_seen, report.placements_seen,
        report.placements_unknown_block,
        report.transitions_counted, report.rows_written,
    )
    return report
