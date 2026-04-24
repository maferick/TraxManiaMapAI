"""Phase 2 PR M — finishability-proof metadata for source maps.

Separate from the generator's internal finishability gate (see
:mod:`src.generation.finishability`). This module tracks
*source-side* evidence: what the map's author / replay corpus /
internal route tell us about whether the base map is actually
finishable in-game. It populates the ``map_finishability_proof``
table.

**Hard boundary** (scope-v0.1 §Level-2 addendum): data here is
evidence, not a bypass. Generated maps MUST still pass the internal
route gate (``src.generation.finishability.run_finishability_gate``)
before ``route_verified`` becomes True. If a base map has every
medal time + a world-record replay but the generator's own gate
rejects it, the generated artifact still says ``route_verified=false``.

Derivation precedence for ``proof_source`` (strongest → weakest):

- ``replay`` — at least one clean/usable replay with
  ``finish_time_ms`` set. Direct finish evidence from a player.
- ``author_time`` — the author set an AuthorTime on the GBX itself.
- ``world_record`` — replays exist on the map but none are marked
  clean; we still carry the fastest finish as a soft signal.
- ``internal_route`` — only our corridor gate says so.
- ``none`` — no evidence of any kind yet.

The renderer picks one of these to show as a badge; the badge maps
to operator-readable labels:

    replay         → "Player validated"
    author_time    → "Author validated"
    world_record   → "Player validated (unverified)"
    internal_route → "Internally verified"
    none           → (no badge)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pymysql.connections import Connection

from src.parsers import SubprocessParser
from src.parsers.errors import ParseStatus
from src.storage.mariadb import cursor
from src.utils.config import code_version

_LOG = logging.getLogger(__name__)


# Proof-source enum values. Matches the migration's ENUM exactly.
PROOF_SOURCE_REPLAY: str = "replay"
PROOF_SOURCE_AUTHOR_TIME: str = "author_time"
PROOF_SOURCE_WORLD_RECORD: str = "world_record"
PROOF_SOURCE_INTERNAL_ROUTE: str = "internal_route"
PROOF_SOURCE_NONE: str = "none"


@dataclass(frozen=True)
class FinishabilityProof:
    """One source map's proof record. Mirrors
    ``map_finishability_proof`` columns 1:1."""
    map_id: int
    author_time_ms: int | None
    bronze_time_ms: int | None
    silver_time_ms: int | None
    gold_time_ms: int | None
    world_record_time_ms: int | None
    world_record_replay_id: int | None
    has_author_time: bool
    has_world_record: bool
    proof_source: str


# ---------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------

def derive_proof_source(
    *,
    has_author_time: bool,
    has_clean_replay: bool,
    has_any_replay: bool,
    has_internal_route: bool,
) -> str:
    """Pure function — pick the strongest proof source from the four
    flags the caller can compute cheaply. Order matches the module
    docstring's precedence."""
    if has_clean_replay:
        return PROOF_SOURCE_REPLAY
    if has_author_time:
        return PROOF_SOURCE_AUTHOR_TIME
    if has_any_replay:
        return PROOF_SOURCE_WORLD_RECORD
    if has_internal_route:
        return PROOF_SOURCE_INTERNAL_ROUTE
    return PROOF_SOURCE_NONE


# ---------------------------------------------------------------------
# DB reads — author/medal times come from re-parsing the GBX; WR +
# internal-route flags come from existing tables.
# ---------------------------------------------------------------------

_MAP_GBX_PATH_SQL = """
SELECT raw_artifact_path
FROM maps
WHERE id = %s
"""

# WR: fastest clean or usable-with-warnings replay with a finish.
# The subquery is cheap (indexed (map_id, clean_status)).
_REPLAY_EVIDENCE_SQL = """
SELECT
    (SELECT id
     FROM replays
     WHERE map_id = %s
       AND clean_status IN ('clean', 'usable_with_warnings')
       AND finish_time_ms IS NOT NULL
     ORDER BY finish_time_ms ASC
     LIMIT 1) AS wr_replay_id,
    (SELECT finish_time_ms
     FROM replays
     WHERE map_id = %s
       AND clean_status IN ('clean', 'usable_with_warnings')
       AND finish_time_ms IS NOT NULL
     ORDER BY finish_time_ms ASC
     LIMIT 1) AS wr_time_ms,
    (SELECT 1
     FROM replays
     WHERE map_id = %s
       AND finish_time_ms IS NOT NULL
     LIMIT 1) AS any_replay
"""

_INTERNAL_ROUTE_SQL = """
SELECT 1 FROM route_corridors
WHERE map_id = %s AND learned_corridor_score IS NOT NULL
LIMIT 1
"""

_UPSERT_SQL = """
INSERT INTO map_finishability_proof (
    map_id,
    author_time_ms, bronze_time_ms, silver_time_ms, gold_time_ms,
    world_record_time_ms, world_record_replay_id,
    has_author_time, has_world_record,
    proof_source,
    created_by_version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    author_time_ms = VALUES(author_time_ms),
    bronze_time_ms = VALUES(bronze_time_ms),
    silver_time_ms = VALUES(silver_time_ms),
    gold_time_ms   = VALUES(gold_time_ms),
    world_record_time_ms = VALUES(world_record_time_ms),
    world_record_replay_id = VALUES(world_record_replay_id),
    has_author_time = VALUES(has_author_time),
    has_world_record = VALUES(has_world_record),
    proof_source = VALUES(proof_source),
    recorded_at = CURRENT_TIMESTAMP(6),
    created_by_version = VALUES(created_by_version)
"""


def _parse_medal_times(
    parser: SubprocessParser, gbx_path: Path,
) -> dict[str, int | None]:
    """Re-parse the GBX just to read its author / medal times.
    Returns a dict with keys author_time_ms / bronze / silver / gold.
    Any value may be None. Parser errors bubble up as exceptions so
    the caller can decide whether to skip the map or fail loud."""
    result = parser.parse_map(gbx_path)
    if result.status is not ParseStatus.SUCCESS:
        raise RuntimeError(
            f"re-parse failed for {gbx_path}: "
            f"{result.error_code.value} {result.error_detail}"
        )
    out = result.output or {}
    return {
        "author_time_ms": _as_int_or_none(out.get("author_time_ms")),
        "bronze_time_ms": _as_int_or_none(out.get("bronze_time_ms")),
        "silver_time_ms": _as_int_or_none(out.get("silver_time_ms")),
        "gold_time_ms":   _as_int_or_none(out.get("gold_time_ms")),
    }


def _as_int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def compute_for_map(
    conn: Connection, map_id: int, *, parser: SubprocessParser,
) -> FinishabilityProof:
    """Compute + persist one map's proof record. Returns the
    :class:`FinishabilityProof` that was written. Raises
    :class:`FileNotFoundError` if the base GBX is missing;
    :class:`RuntimeError` on parser failure."""
    with cursor(conn) as cur:
        cur.execute(_MAP_GBX_PATH_SQL, (map_id,))
        row = cur.fetchone()
        if row is None or not row[0]:
            raise FileNotFoundError(
                f"map_id={map_id} has no raw_artifact_path on record"
            )
        gbx_path = Path(str(row[0]))
        if not gbx_path.is_absolute():
            gbx_path = Path.cwd() / gbx_path
        if not gbx_path.exists():
            raise FileNotFoundError(
                f"map_id={map_id} raw_artifact_path missing on disk: {gbx_path}"
            )

        medals = _parse_medal_times(parser, gbx_path)

        cur.execute(_REPLAY_EVIDENCE_SQL, (map_id, map_id, map_id))
        r = cur.fetchone()
        wr_replay_id = _as_int_or_none(r[0] if r else None)
        wr_time_ms = _as_int_or_none(r[1] if r else None)
        has_any_replay = bool(r and r[2])
        has_clean_replay = wr_time_ms is not None

        cur.execute(_INTERNAL_ROUTE_SQL, (map_id,))
        has_internal_route = cur.fetchone() is not None

    has_author_time = medals["author_time_ms"] is not None
    has_world_record = has_clean_replay

    proof_source = derive_proof_source(
        has_author_time=has_author_time,
        has_clean_replay=has_clean_replay,
        has_any_replay=has_any_replay,
        has_internal_route=has_internal_route,
    )

    with cursor(conn) as cur:
        cur.execute(
            _UPSERT_SQL,
            (
                map_id,
                medals["author_time_ms"],
                medals["bronze_time_ms"],
                medals["silver_time_ms"],
                medals["gold_time_ms"],
                wr_time_ms,
                wr_replay_id,
                int(has_author_time),
                int(has_world_record),
                proof_source,
                code_version(),
            ),
        )
    conn.commit()

    proof = FinishabilityProof(
        map_id=map_id,
        author_time_ms=medals["author_time_ms"],
        bronze_time_ms=medals["bronze_time_ms"],
        silver_time_ms=medals["silver_time_ms"],
        gold_time_ms=medals["gold_time_ms"],
        world_record_time_ms=wr_time_ms,
        world_record_replay_id=wr_replay_id,
        has_author_time=has_author_time,
        has_world_record=has_world_record,
        proof_source=proof_source,
    )
    _LOG.info(
        "finishability_proof: map_id=%d author=%sms wr=%sms source=%s",
        map_id, medals["author_time_ms"], wr_time_ms, proof_source,
    )
    return proof


# ---------------------------------------------------------------------
# Read path for UI / other callers
# ---------------------------------------------------------------------

_FETCH_SQL = """
SELECT map_id, author_time_ms, bronze_time_ms, silver_time_ms, gold_time_ms,
       world_record_time_ms, world_record_replay_id,
       has_author_time, has_world_record, proof_source
FROM map_finishability_proof
WHERE map_id = %s
"""


def fetch_proof(conn: Connection, map_id: int) -> FinishabilityProof | None:
    """Read the persisted proof row for a map. Returns None if no row
    exists (the compute step hasn't run for this map yet)."""
    with cursor(conn) as cur:
        cur.execute(_FETCH_SQL, (map_id,))
        r = cur.fetchone()
    if r is None:
        return None
    return FinishabilityProof(
        map_id=int(r[0]),
        author_time_ms=_as_int_or_none(r[1]),
        bronze_time_ms=_as_int_or_none(r[2]),
        silver_time_ms=_as_int_or_none(r[3]),
        gold_time_ms=_as_int_or_none(r[4]),
        world_record_time_ms=_as_int_or_none(r[5]),
        world_record_replay_id=_as_int_or_none(r[6]),
        has_author_time=bool(r[7]),
        has_world_record=bool(r[8]),
        proof_source=str(r[9]),
    )
