"""Phase 2 #218-1 — block-transition pair-count extractor.

Reads driven-through path cells from ``route_corridors`` and counts
ordered ``(family_a, name_a) → (family_b, name_b)`` transitions per
environment, persisting the result to ``block_pair_transitions``.

Why path cells and not raw ``block_placements`` adjacency: path cells
are already "this pair is evidence along a routable chain" — raw grid
neighbours would include decorative stacks and structural support
that no driver ever crosses, drowning the pattern signal.

Scope per project-218 doc: used as a weighting signal in generation
and a warning signal for rare transitions. **Never** a hard constraint
— see the composition rule in #218-4 that prevents frequency from
overriding traversability evidence.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from pymysql.connections import Connection

from src.storage.mariadb import cursor
from src.utils.config import code_version

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PairKey:
    family_a: str
    name_a: str
    family_b: str
    name_b: str
    environment: str


@dataclass(frozen=True)
class _TripleKey:
    family_a: str
    name_a: str
    family_b: str
    name_b: str
    family_c: str
    name_c: str
    environment: str

    def signature(self) -> str:
        """Client-computed PK for block_triple_transitions. The
        natural composite key (7 varchars) overflows MariaDB's 3072-
        byte index limit; sha256 of a joined string keeps the index
        small while staying collision-safe for our data scale."""
        payload = "|".join((
            self.family_a, self.name_a,
            self.family_b, self.name_b,
            self.family_c, self.name_c,
            self.environment,
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class BuildReport:
    maps_seen: int = 0
    corridors_seen: int = 0
    transitions_counted: int = 0
    pairs_written: int = 0
    triple_transitions_counted: int = 0
    triples_written: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


# ---------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------

# All route_corridors rows for a set of maps, joined to their
# environment. We only read path_cells — everything else is indexing.
_CORRIDOR_PATHS_SQL = """
SELECT rc.map_id, COALESCE(m.environment, '') AS environment, rc.path_cells
FROM route_corridors rc
JOIN maps m ON m.id = rc.map_id
WHERE rc.map_id IN ({placeholders})
  AND rc.path_cells IS NOT NULL
"""

# Block_placements indexed by (map_id, cell) for the lookup. Free-
# placed blocks are skipped — pair transitions are grid-semantic,
# free blocks (start/finish gates mostly) don't carry the same
# "next-to" meaning we're quantifying.
_BLOCKS_AT_CELLS_SQL = """
SELECT block_family, block_type, x, y, z
FROM block_placements
WHERE map_id = %s AND is_free = 0
"""

_SCAN_MAP_IDS_SQL = """
SELECT DISTINCT map_id
FROM route_corridors
WHERE path_cells IS NOT NULL
ORDER BY map_id
{limit_clause}
"""

_UPSERT_PAIR_SQL = """
INSERT INTO block_pair_transitions (
    block_family_a, block_name_a,
    block_family_b, block_name_b,
    environment, transition_count, map_count,
    created_by_version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    transition_count = transition_count + VALUES(transition_count),
    map_count        = map_count        + VALUES(map_count),
    updated_at       = CURRENT_TIMESTAMP(6),
    created_by_version = VALUES(created_by_version)
"""

_UPSERT_TRIPLE_SQL = """
INSERT INTO block_triple_transitions (
    transition_signature,
    block_family_a, block_name_a,
    block_family_b, block_name_b,
    block_family_c, block_name_c,
    environment, transition_count, map_count,
    created_by_version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    transition_count = transition_count + VALUES(transition_count),
    map_count        = map_count        + VALUES(map_count),
    updated_at       = CURRENT_TIMESTAMP(6),
    created_by_version = VALUES(created_by_version)
"""


# ---------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------

def _parse_path_cells(raw: str | None) -> list[tuple[int, int, int]]:
    """route_corridors.path_cells is a JSON array of 3-element int
    lists. Return a list of (x, y, z) tuples or an empty list on
    anything we can't parse — a malformed row shouldn't tank the
    whole build."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    out: list[tuple[int, int, int]] = []
    for c in data:
        if isinstance(c, (list, tuple)) and len(c) == 3:
            try:
                out.append((int(c[0]), int(c[1]), int(c[2])))
            except (TypeError, ValueError):
                continue
    return out


def _fetch_blocks_by_cell(
    conn: Connection, map_id: int,
) -> dict[tuple[int, int, int], tuple[str, str]]:
    """Cell → (block_family, block_type) for grid-placed blocks on
    one map. Multiple blocks at the same cell are rare but possible
    (stacked structural support); last-write-wins is fine for the
    pair-count signal, which doesn't care about stack depth."""
    out: dict[tuple[int, int, int], tuple[str, str]] = {}
    with cursor(conn) as cur:
        cur.execute(_BLOCKS_AT_CELLS_SQL, (map_id,))
        for family, name, x, y, z in cur.fetchall():
            if x is None or y is None or z is None:
                continue
            out[(int(x), int(y), int(z))] = (
                str(family), str(name),
            )
    return out


def extract_triple_counts_for_map(
    conn: Connection, map_id: int,
) -> dict[_TripleKey, int]:
    """Per-map triple counts. For each consecutive 3-cell window in
    every corridor's path_cells, emit one ordered (A, B, C) triple.
    Windows containing any unknown cell are skipped silently — a
    3-element path with one unknown cell contributes zero triples
    rather than spreading partial evidence. Same direction-matters
    contract as pairs."""
    with cursor(conn) as cur:
        cur.execute(
            _CORRIDOR_PATHS_SQL.format(placeholders="%s"),
            (map_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return {}

    cell_to_block = _fetch_blocks_by_cell(conn, map_id)

    counts: dict[_TripleKey, int] = defaultdict(int)
    for _map_id, env, path_cells_raw in rows:
        cells = _parse_path_cells(path_cells_raw)
        # Window of 3: cells[i], cells[i+1], cells[i+2].
        for i in range(len(cells) - 2):
            a = cell_to_block.get(cells[i])
            b = cell_to_block.get(cells[i + 1])
            c = cell_to_block.get(cells[i + 2])
            if a is None or b is None or c is None:
                continue
            key = _TripleKey(
                family_a=a[0], name_a=a[1],
                family_b=b[0], name_b=b[1],
                family_c=c[0], name_c=c[1],
                environment=str(env),
            )
            counts[key] += 1
    return counts


def extract_pair_counts_for_map(
    conn: Connection, map_id: int,
) -> dict[_PairKey, int]:
    """Per-map pair counts. Each consecutive cell pair in each
    corridor's path_cells contributes one ordered transition — if
    both cells have a known block. Unknown cells (no block placement
    at that grid position — can happen with free-placed anchors or
    snapped-to-grid synthesized cells) are skipped silently."""
    with cursor(conn) as cur:
        cur.execute(
            _CORRIDOR_PATHS_SQL.format(placeholders="%s"),
            (map_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return {}

    environment = str(rows[0][1]) if rows else ""
    cell_to_block = _fetch_blocks_by_cell(conn, map_id)

    counts: dict[_PairKey, int] = defaultdict(int)
    for _map_id, env, path_cells_raw in rows:
        cells = _parse_path_cells(path_cells_raw)
        for i in range(len(cells) - 1):
            a = cell_to_block.get(cells[i])
            b = cell_to_block.get(cells[i + 1])
            if a is None or b is None:
                continue
            key = _PairKey(
                family_a=a[0], name_a=a[1],
                family_b=b[0], name_b=b[1],
                environment=str(env),
            )
            counts[key] += 1
    return counts


# ---------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------

def _persist_pair_counts(
    conn: Connection, counts: dict[_PairKey, int],
) -> int:
    """Upsert counts into ``block_pair_transitions``. Each distinct
    (a, b, env) from this call bumps map_count by 1 and transition_count
    by the in-batch value. Returns the number of distinct pair rows
    written."""
    if not counts:
        return 0
    sha = code_version()
    rows = [
        (
            k.family_a, k.name_a, k.family_b, k.name_b,
            k.environment, int(n), 1, sha,
        )
        for k, n in counts.items()
    ]
    with cursor(conn) as cur:
        cur.executemany(_UPSERT_PAIR_SQL, rows)
    conn.commit()
    return len(rows)


def _persist_triple_counts(
    conn: Connection, counts: dict[_TripleKey, int],
) -> int:
    """Upsert counts into ``block_triple_transitions``. PK is the
    client-computed sha256 signature; see ``_TripleKey.signature``."""
    if not counts:
        return 0
    sha = code_version()
    rows = [
        (
            k.signature(),
            k.family_a, k.name_a,
            k.family_b, k.name_b,
            k.family_c, k.name_c,
            k.environment, int(n), 1, sha,
        )
        for k, n in counts.items()
    ]
    with cursor(conn) as cur:
        cur.executemany(_UPSERT_TRIPLE_SQL, rows)
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------

def build_block_transition_counts(
    conn: Connection,
    *,
    map_ids: Iterable[int] | None = None,
    limit: int | None = None,
    include_triples: bool = True,
) -> BuildReport:
    """Scan ``route_corridors`` for the given maps (or every map with
    path_cells when ``map_ids`` is None) and populate the pair +
    (optionally) triple transition tables. Existing counts ACCUMULATE
    — use :func:`reset_transition_counts` for a clean rebuild.

    Pairs and triples share an extraction pass over the corridor
    rows: the per-map pair and triple dicts are both built from the
    same ``cell_to_block`` lookup, so there's no extra DB traffic for
    the triple layer.
    """
    report = BuildReport()
    if map_ids is None:
        limit_clause = f"LIMIT {int(limit)}" if limit else ""
        with cursor(conn) as cur:
            cur.execute(_SCAN_MAP_IDS_SQL.format(limit_clause=limit_clause))
            target_ids = [int(row[0]) for row in cur.fetchall()]
    else:
        target_ids = [int(m) for m in map_ids]

    report.maps_seen = len(target_ids)
    for mid in target_ids:
        try:
            pair_counts = extract_pair_counts_for_map(conn, mid)
            with cursor(conn) as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM route_corridors "
                    "WHERE map_id = %s AND path_cells IS NOT NULL",
                    (mid,),
                )
                c_row = cur.fetchone()
                report.corridors_seen += int(c_row[0]) if c_row else 0
            report.pairs_written += _persist_pair_counts(conn, pair_counts)
            report.transitions_counted += sum(pair_counts.values())
            if include_triples:
                triple_counts = extract_triple_counts_for_map(conn, mid)
                report.triples_written += _persist_triple_counts(
                    conn, triple_counts,
                )
                report.triple_transitions_counted += sum(
                    triple_counts.values()
                )
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"map_id={mid}: {exc}")
            _LOG.exception("build_block_transition_counts failed on map %d", mid)

    _LOG.info(
        "build_block_transition_counts: maps=%d corridors=%d "
        "pair_transitions=%d pair_rows=%d "
        "triple_transitions=%d triple_rows=%d errors=%d",
        report.maps_seen, report.corridors_seen,
        report.transitions_counted, report.pairs_written,
        report.triple_transitions_counted, report.triples_written,
        len(report.errors),
    )
    return report


def reset_transition_counts(conn: Connection) -> None:
    """TRUNCATE both pair and triple transition tables. Used by the
    CLI ``--reset`` flag when the operator wants a clean rebuild."""
    with cursor(conn) as cur:
        cur.execute("TRUNCATE TABLE block_pair_transitions")
        cur.execute("TRUNCATE TABLE block_triple_transitions")
    conn.commit()
    _LOG.info("block pair + triple transitions: truncated")


# Backwards-compatibility shims for the #218-1 entry points. Delete
# these once the CLI verb is renamed away from "pair-counts" (next PR
# in the arc: unify under `build-block-transitions`).
build_block_pair_counts = build_block_transition_counts
reset_pair_counts = reset_transition_counts
