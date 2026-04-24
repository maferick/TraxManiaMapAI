"""Minimal base-map generator — Phase 2 PR E.

Per scope-v0:

> PR E — minimal generator: takes a base_map_id, emits a modified JSON
>   (copy the base's blocks, run assembly + gate on it, emit the v0
>   schema). Answers "does the pipeline produce a JSON file that the
>   gate validates?" — not "is the output good."

This is deliberately narrow:

- Input: ``base_map_id`` (existing parsed map with block_placements).
- Actions: fetch blocks + checkpoints + provenance, call
  :func:`src.generation.assemble_route`, call
  :func:`src.generation.run_finishability_gate`, build a v0 JSON
  artifact, validate against the bundled schema, return the dict.
- No novelty in the output — the v0 generator's "generated" map is
  a faithful copy of the base. Real generation (mutation / scratch
  / style transfer) ships in PR G+ with its own scope-doc revision.
- No style/difficulty heuristics — those fields are recorded in
  ``inputs`` but don't affect assembly in v0.

What this module guarantees:

- The output dict validates against
  ``src/generation/generated_map.schema.json``.
- ``run_id`` is deterministic sha over the inputs block; same inputs
  → same run_id, bit-for-bit.
- Provenance fields are all populated (required by the schema).
- The finishability block reflects what the gate actually returned,
  including reject_reason + detail when the gate rejected.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pymysql.connections import Connection

from src.corridor.traversability.classification import CLASSIFICATION_VERSION
from src.generation.assembly import assemble_route
from src.generation.finishability import run_finishability_gate
from src.generation.schema import validate_generated_map
from src.generation.types import (
    AssembledRoute,
    AssemblyError,
    FinishabilityResult,
)
from src.storage.mariadb import cursor
from src.utils.config import code_version, resolve_config_hash

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------

_ALLOWED_STYLE = {"Tech", "FullSpeed", None}
_ALLOWED_DIFFICULTY = {"easy", "medium", "hard"}

# Level-1 mutation: assembly picks deterministically from the top-K
# tie-break-ordered candidates per interval, keyed on (random_seed,
# interval_index). K=3 balances "meaningful variation" against
# "quality stays near the top of the learned ranking." Bumping this
# knob is a v0.1+ tuning exercise; for v0 it's pinned.
_TOP_K_CANDIDATES: int = 3


@dataclass(frozen=True)
class GenerationInputs:
    """Operator-facing knobs for a single generation run. Matches the
    ``inputs`` block of the v0 artifact 1:1 so serialization is a
    pass-through.

    ``strip`` toggles Level-2 strip-to-route (scope-v0.1): when True,
    the artifact carries only the blocks along the chosen route + a
    small halo rather than a full copy of the base. Flows into the
    run_id hash so ``(seed=42, strip=False)`` and ``(seed=42, strip=True)``
    produce distinct artifacts.
    """
    base_map_id: int | None
    base_map_source_id: str | None
    style_tag_filter: str | None = None
    difficulty: str = "medium"
    random_seed: int = 42
    strip: bool = False

    def __post_init__(self) -> None:
        if self.style_tag_filter not in _ALLOWED_STYLE:
            raise ValueError(
                f"style_tag_filter {self.style_tag_filter!r} must be "
                f"one of {sorted(s for s in _ALLOWED_STYLE if s is not None)} or None"
            )
        if self.difficulty not in _ALLOWED_DIFFICULTY:
            raise ValueError(
                f"difficulty {self.difficulty!r} must be one of "
                f"{sorted(_ALLOWED_DIFFICULTY)}"
            )


def _compute_run_id(inputs: GenerationInputs) -> str:
    """Deterministic 16-hex sha over the inputs block. Same inputs →
    same run_id, as scope-v0 §Provenance requires."""
    payload = {
        "base_map_id": inputs.base_map_id,
        "base_map_source_id": inputs.base_map_source_id,
        "style_tag_filter": inputs.style_tag_filter,
        "difficulty": inputs.difficulty,
        "random_seed": inputs.random_seed,
        "strip": inputs.strip,
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class _BaseMapData:
    """Everything we need from the DB to build the artifact for one
    base map. Kept compact so unit tests can construct it directly."""
    source_map_id: str | None
    blocks: list[dict[str, Any]]
    checkpoints: list[dict[str, Any]]
    # Union of all grid cells occupied by every waypoint — grid-placed
    # cells directly, free-placed cells after snapping via TM2020's
    # fixed block-size grid. Level-2 strip uses this to preserve
    # structural geometry around anchors under the
    # halo_axis_1_plus_anchor_radius_3 policy. An empty set is
    # perfectly valid (matches the grid-only scope-v0 invariant).
    anchor_cells: frozenset[tuple[int, int, int]]
    # Provenance from scored-corridor rows (consistent across the map
    # if all corridors share one model hash, which they do when
    # score-corridors-learned ran cleanly).
    model_hash: str | None
    learned_score_version: str | None


_MAP_META_SQL = """
SELECT source_map_id
FROM maps
WHERE id = %s
"""

_BLOCKS_SQL = """
SELECT block_family, block_type, x, y, z, rotation
FROM block_placements
WHERE map_id = %s AND is_free = 0
ORDER BY placement_index ASC
"""

_CHECKPOINTS_SQL = """
SELECT waypoint_index, waypoint_order, tag, x, y, z,
       abs_x, abs_y, abs_z, placement
FROM map_checkpoints
WHERE map_id = %s
  AND tag IN ('Spawn', 'Checkpoint', 'LinkedCheckpoint', 'Goal')
ORDER BY
    CASE tag
        WHEN 'Spawn' THEN 0
        WHEN 'Checkpoint' THEN 1
        WHEN 'LinkedCheckpoint' THEN 1
        WHEN 'Goal' THEN 2
    END,
    waypoint_order,
    waypoint_index
"""


# TM2020 grid cell dimensions (metres). Verified empirically from
# map 1212: Spawn abs=(752, 24, 128) snaps to cell (23, 3, 4) →
# 752/32=23.5, 24/8=3, 128/32=4. These are the canonical native block
# dimensions and are invariant per environment.
_BLOCK_SIZE_X: int = 32
_BLOCK_SIZE_Y: int = 8
_BLOCK_SIZE_Z: int = 32


def _snap_abs_to_grid(
    abs_x: float, abs_y: float, abs_z: float,
) -> tuple[int, int, int]:
    """Map an absolute (x, y, z) in metres to a TM2020 grid cell.
    Floor-divided so free-placed waypoints land in the cell they're
    nominally inside. See _BLOCK_SIZE_{X,Y,Z}."""
    return (
        int(abs_x // _BLOCK_SIZE_X),
        int(abs_y // _BLOCK_SIZE_Y),
        int(abs_z // _BLOCK_SIZE_Z),
    )

_PROVENANCE_SQL = """
SELECT learned_score_model_hash, learned_score_version, COUNT(*)
FROM route_corridors
WHERE map_id = %s AND learned_corridor_score IS NOT NULL
GROUP BY learned_score_model_hash, learned_score_version
ORDER BY COUNT(*) DESC
LIMIT 1
"""


def _fetch_base_map(conn: Connection, map_id: int) -> _BaseMapData:
    """Pull blocks + checkpoints + provenance for the given base map.
    Raises :class:`ValueError` if the map doesn't exist — callers
    treat that as an operator error, not a schema reject."""
    with cursor(conn) as cur:
        cur.execute(_MAP_META_SQL, (map_id,))
        meta = cur.fetchone()
        if meta is None:
            raise ValueError(f"base map_id={map_id} not found")
        source_map_id = str(meta[0]) if meta[0] is not None else None

        cur.execute(_BLOCKS_SQL, (map_id,))
        block_rows = cur.fetchall()

        cur.execute(_CHECKPOINTS_SQL, (map_id,))
        cp_rows = cur.fetchall()

        cur.execute(_PROVENANCE_SQL, (map_id,))
        prov = cur.fetchone()

    blocks: list[dict[str, Any]] = [
        {
            "block_family": str(r[0]) if r[0] is not None else "Unknown",
            "block_name": str(r[1]) if r[1] is not None else "Unknown",
            "x": int(r[2]), "y": int(r[3]), "z": int(r[4]),
            "rotation": int(r[5]) if r[5] is not None else 0,
        }
        for r in block_rows
    ]
    # Free-placed waypoints (placement='free') store positions in
    # abs_x/y/z rather than grid x/y/z, so the grid x/y/z we selected
    # are NULL. We skip them from the schema-emitted `checkpoints`
    # list — scope-v0 §map.checkpoints requires integer grid coords
    # and route.intervals + route.corridors_used already carry their
    # snapped cells. But PR L adds a second use: the Level-2 strip
    # policy needs anchor cells to preserve the structural geometry
    # around Spawn / CP / Goal blocks. For that we snap free-placed
    # waypoints to grid via the TM2020 block-size constants and
    # collect *all* anchor cells (grid + snapped) into
    # ``_BaseMapData.anchor_cells``.
    checkpoints: list[dict[str, Any]] = []
    anchor_cells_set: set[tuple[int, int, int]] = set()
    skipped_free = 0
    for r in cp_rows:
        (
            waypoint_index, waypoint_order, tag, x, y, z,
            abs_x, abs_y, abs_z, placement,
        ) = r
        if x is not None and y is not None and z is not None:
            cell = (int(x), int(y), int(z))
            anchor_cells_set.add(cell)
            checkpoints.append({
                "waypoint_index": int(waypoint_index),
                "waypoint_order": int(waypoint_order),
                "tag": str(tag),
                "x": cell[0], "y": cell[1], "z": cell[2],
            })
        elif abs_x is not None and abs_y is not None and abs_z is not None:
            # Free-placed → snap to grid. Kept out of the artifact's
            # `checkpoints` (schema constraint) but included in the
            # strip-time anchor set.
            snapped = _snap_abs_to_grid(
                float(abs_x), float(abs_y), float(abs_z),
            )
            anchor_cells_set.add(snapped)
            skipped_free += 1
        else:
            # Row has neither grid nor abs coords — parse anomaly.
            skipped_free += 1
    if skipped_free:
        _LOG.info(
            "base map_id=%d: %d free-placed waypoint(s) omitted from "
            "artifact.map.checkpoints (scope-v0 grid-only constraint); "
            "snapped cells captured for strip anchor-radius preservation",
            map_id, skipped_free,
        )

    model_hash = None
    learned_score_version = None
    if prov is not None:
        model_hash = str(prov[0]) if prov[0] is not None else None
        learned_score_version = str(prov[1]) if prov[1] is not None else None

    return _BaseMapData(
        source_map_id=source_map_id,
        blocks=blocks,
        checkpoints=checkpoints,
        anchor_cells=frozenset(anchor_cells_set),
        model_hash=model_hash,
        learned_score_version=learned_score_version,
    )


# ---------------------------------------------------------------------
# Artifact construction
# ---------------------------------------------------------------------

def _interval_entries(
    route: AssembledRoute | AssemblyError,
) -> list[dict[str, Any]]:
    """Build the ``route.intervals[]`` array. Empty when the gate
    rejected pre-assembly (no chosen corridors to describe)."""
    if isinstance(route, AssemblyError):
        return []
    return [
        {
            "index": iv.index,
            "src_tag": iv.src.tag,
            "src_order": iv.src.order,
            "dst_tag": iv.dst.tag,
            "dst_order": iv.dst.order,
            "chosen_corridor_id": iv.chosen.corridor_id,
            "chosen_corridor_score": iv.chosen.learned_corridor_score,
            "path_length_cells": iv.chosen.path_length,
            "expected_time_ms": iv.chosen.expected_time_ms,
        }
        for iv in route.intervals
    ]


def _corridors_used_entries(
    route: AssembledRoute | AssemblyError,
) -> list[dict[str, Any]]:
    if isinstance(route, AssemblyError):
        return []
    out: list[dict[str, Any]] = []
    for iv in route.intervals:
        c = iv.chosen
        entry: dict[str, Any] = {
            "corridor_id": c.corridor_id,
            "interval_index": iv.index,
            "learned_corridor_score": c.learned_corridor_score,
            "contains_virtual_edge": c.contains_virtual_edge,
            "path_length_cells": c.path_length,
        }
        if c.corridor_confidence is not None:
            entry["corridor_confidence"] = c.corridor_confidence
        # #218-5 diagnostic — optional field, emitted only when the
        # corpus has been scored.
        if c.combined_sequence_score is not None:
            entry["combined_sequence_score"] = c.combined_sequence_score
        out.append(entry)
    return out


def _finishability_block(result: FinishabilityResult) -> dict[str, Any]:
    """Serialize FinishabilityResult to the artifact's finishability
    shape. Mirror the dataclass 1:1 with the schema's required set."""
    block: dict[str, Any] = {
        "route_verified": result.route_verified,
        "estimated_time_ms": result.estimated_time_ms,
        "ai_confidence": result.ai_confidence,
        "reject_reason": result.reject_reason,
        "gate_version": result.gate_version,
    }
    if result.detail is not None:
        block["detail"] = result.detail
    return block


def _build_artifact(
    *,
    inputs: GenerationInputs,
    base: _BaseMapData,
    route: AssembledRoute | AssemblyError,
    gate: FinishabilityResult,
    config_hash: str,
    sha: str,
    strip_metadata: dict[str, Any] | None = None,
    stripped_blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the full JSON artifact. Always produces a schema-
    conforming structure — even on reject, the artifact lists the
    base map's blocks/checkpoints (so the operator can still inspect
    what was attempted).

    ``strip_metadata`` + ``stripped_blocks`` ride the Level-2 path:
    when provided, ``map.blocks`` is the stripped subset, the schema
    version bumps to ``generation-v0.1``, and the strip bookkeeping
    (``stripped`` / ``strip_policy`` / ``kept_block_count`` /
    ``base_block_count``) lands on the ``map`` object.
    """
    if route is None or isinstance(route, AssemblyError):
        cells_total = 0
        intervals_produced: list[dict[str, Any]] = []
    else:
        cells_total = route.cells_total
        intervals_produced = _interval_entries(route)

    # scope-v0: interval_count matches len(route.intervals). On the
    # happy path we mirror the actual chain length. On reject we count
    # *logical* anchors (deduped by (tag, waypoint_order)) since
    # multi-cell CPs emit one row per cell — counting rows would inflate
    # the interval_count beyond what the chain actually represents.
    if intervals_produced:
        interval_count = len(intervals_produced)
    else:
        logical_anchors = {
            (c["tag"], c["waypoint_order"]) for c in base.checkpoints
        }
        interval_count = max(1, len(logical_anchors) - 1)

    schema_version = "generation-v0.1" if strip_metadata else "generation-v0"
    blocks_out = (
        stripped_blocks if stripped_blocks is not None else base.blocks
    )

    map_block: dict[str, Any] = {
        "waypoint_order_style": "linked",
        "interval_count": interval_count,
        "blocks": blocks_out,
        "checkpoints": base.checkpoints,
    }
    if strip_metadata is not None:
        map_block.update(strip_metadata)

    return {
        "schema_version": schema_version,
        "run_id": _compute_run_id(inputs),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "inputs": {
            "base_map_id": inputs.base_map_id,
            "base_map_source_id": inputs.base_map_source_id,
            "style_tag_filter": inputs.style_tag_filter,
            "difficulty": inputs.difficulty,
            "random_seed": inputs.random_seed,
            "strip": inputs.strip,
        },
        "provenance": {
            "model_hash": base.model_hash or ("0" * 64),
            "learned_score_version": (
                base.learned_score_version or "unscored"
            ),
            "config_hash": config_hash,
            "code_version": sha,
            "classification_version": CLASSIFICATION_VERSION,
        },
        "map": map_block,
        "route": {
            "intervals": intervals_produced,
            "cells_total": cells_total,
            "corridors_used": _corridors_used_entries(route),
        },
        "finishability": _finishability_block(gate),
    }


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def generate_from_base(
    conn: Connection,
    *,
    inputs: GenerationInputs,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Produce one v0 generated-map artifact for the given inputs.

    Fetches base data, runs assembly, runs the finishability gate,
    builds the JSON dict, validates it against the bundled schema,
    returns it.

    Errors:
      - ``ValueError`` if ``inputs.base_map_id`` is None (v0 doesn't
        support scratch generation — requires a base) or the
        base_map_id doesn't exist.
      - ``RuntimeError`` if the assembled artifact fails schema
        validation (a real bug — scope-doc drift between scope-v0
        and this module).

    Does NOT raise on finishability-gate rejections. Those are normal
    outcomes; the artifact carries `route_verified=False` plus the
    reject_reason, and is returned as-is.
    """
    if inputs.base_map_id is None:
        raise ValueError(
            "v0 generation requires a base_map_id — scratch generation "
            "is deferred to v0.1+"
        )

    sha = code_version()
    config_hash = resolve_config_hash(config)

    base = _fetch_base_map(conn, inputs.base_map_id)
    # Backfill input.base_map_source_id if the caller didn't specify.
    effective_inputs = inputs
    if effective_inputs.base_map_source_id is None and base.source_map_id:
        effective_inputs = GenerationInputs(
            base_map_id=inputs.base_map_id,
            base_map_source_id=base.source_map_id,
            style_tag_filter=inputs.style_tag_filter,
            difficulty=inputs.difficulty,
            random_seed=inputs.random_seed,
            strip=inputs.strip,
        )

    route = assemble_route(
        conn, inputs.base_map_id,
        random_seed=inputs.random_seed,
        top_k_candidates=_TOP_K_CANDIDATES,
    )
    gate = run_finishability_gate(route)

    # Level-2 strip-to-route. Only runs on a successful assembly
    # (AssembledRoute, not AssemblyError); reject-path artifacts have
    # no chosen corridors to strip around, so they ship with the full
    # base unchanged and schema_version stays generation-v0.
    strip_metadata: dict[str, Any] | None = None
    stripped_blocks: list[dict[str, Any]] | None = None
    if inputs.strip and isinstance(route, AssembledRoute):
        from src.generation.stripper import (
            STRIP_POLICY_HALO_PRISM_3X7X3_PLUS_ANCHOR_RADIUS_3,
            strip_route,
        )
        # #217-c default: a full 3×7×3 prism per path cell. #217-b's
        # XZ-cheb-1 at same Y still left 16 blocks dropped at y±1
        # from route cell (31, 13, 22) on map 1212; in-game test
        # kept failing. Prism = the 3×3 XZ neighbourhood at every Y
        # in the ±3 range. Subsumes xz_cheb_1 + vext_3 into one
        # volume. Earlier policies stay available for reproducibility.
        strip_result = strip_route(
            route, base.blocks,
            policy=STRIP_POLICY_HALO_PRISM_3X7X3_PLUS_ANCHOR_RADIUS_3,
            anchor_cells=base.anchor_cells,
        )
        strip_metadata = {
            "stripped": True,
            "strip_policy": strip_result.strip_policy,
            "kept_block_count": strip_result.kept_block_count,
            "base_block_count": strip_result.base_block_count,
        }
        stripped_blocks = strip_result.stripped_blocks
        # Gate re-run on the stripped shape. Override the gate verdict
        # if the halo wasn't wide enough to preserve the chosen path —
        # route still drives under the FULL map, but once we strip,
        # the ribbon we save is no longer finishable. Surface that as
        # a distinct reject_reason so the operator can diagnose.
        if not strip_result.route_intact:
            gate = FinishabilityResult(
                route_verified=False,
                estimated_time_ms=gate.estimated_time_ms,
                ai_confidence=gate.ai_confidence,
                reject_reason="stripped_route_broken",
                gate_version=gate.gate_version,
                detail=strip_result.broken_detail,
            )

    artifact = _build_artifact(
        inputs=effective_inputs,
        base=base,
        route=route,
        gate=gate,
        config_hash=config_hash,
        sha=sha,
        strip_metadata=strip_metadata,
        stripped_blocks=stripped_blocks,
    )

    err = validate_generated_map(artifact)
    if err is not None:
        raise RuntimeError(
            f"generated artifact failed schema validation: {err}"
        )

    _LOG.info(
        "generate_from_base: run_id=%s base_map_id=%d "
        "route_verified=%s reject=%s ai_confidence=%s",
        artifact["run_id"],
        inputs.base_map_id,
        gate.route_verified,
        gate.reject_reason,
        f"{gate.ai_confidence:.3f}" if gate.ai_confidence is not None else "n/a",
    )
    return artifact
