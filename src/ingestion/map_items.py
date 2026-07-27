"""Ingest item (anchored-object) placements into ``map_items``.

Transform and identity only. Item geometry is not resolved because none
is available offline: the block catalogue covers blocks, and there is no
item catalogue. Whether resolving it is worth the work is an OPEN
question, not a settled one. Anchor-only matching recovered little
surface coverage on the pilot, but that shows an anchor cell is a poor
proxy for a large item's extent, not that the item is irrelevant: a
trajectory may be riding item surfaces whose anchors sit cells away.

What this table IS justified by today is route pinning. On item-built
maps the waypoints are items, and adding item anchors moved
checkpoint-region coverage from 0.0% to 65.6% on one pilot map and from
70% to 100% on another.

Volume: 15,262,391 items across the 18,935-map Stadium2020 corpus,
646,125 for the 545-map gold set. Populate the captured cohort first;
a full backfill is a separate decision with its own disk check.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

_LOG = logging.getLogger(__name__)

# Grid calibration, same constants the block matcher uses. Kept in sync
# deliberately rather than imported, because this module has to be
# usable during ingestion before any route code is loaded.
CELL_SIZE_M = 32.0
LEVEL_HEIGHT_M = 8.0
GROUND_ROW = 9
GROUND_ABS_Y_M = 8.0


@dataclass
class ItemIngestStats:
    maps: int = 0
    items: int = 0
    waypoint_items: int = 0
    failed_maps: int = 0
    unit_coord_mismatches: int = 0
    baked: int = 0
    baked_terrain: int = 0
    failures: list[str] = field(default_factory=list)


def derive_cell(
    abs_x: float | None, abs_y: float | None, abs_z: float | None
) -> tuple[int | None, int | None, int | None]:
    """Grid cell derived from absolute metres, via the calibration."""
    if abs_x is None or abs_y is None or abs_z is None:
        return (None, None, None)
    return (
        math.floor(abs_x / CELL_SIZE_M),
        math.floor(GROUND_ROW + (abs_y - GROUND_ABS_Y_M) / LEVEL_HEIGHT_M),
        math.floor(abs_z / CELL_SIZE_M),
    )


# BlockUnitCoord is a byte triple and 255 is its "unset" sentinel: an
# item only carries a real one when it is snapped to a block position.
# MEASURED on the captured cohort: 8,655 of 20,817 items (41.6%) report
# 255. Storing that verbatim would put those items at cell 255, i.e.
# nowhere, and silently poison item candidate generation. Where it IS
# set it confirms the grid calibration: 11,488 of 12,162 agree exactly
# with the cell derived from the absolute position, and the remainder
# differ by exactly one cell at a boundary.
BLOCK_UNIT_UNSET = 255


def _block_unit(item: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    vals = (item.get("cell_x"), item.get("cell_y"), item.get("cell_z"))
    if any(v is None or v >= BLOCK_UNIT_UNSET for v in vals):
        return (None, None, None)
    return vals


def _row(item: dict[str, Any], index: int) -> tuple:
    abs_x, abs_y, abs_z = item.get("abs_x"), item.get("abs_y"), item.get("abs_z")
    cell = derive_cell(abs_x, abs_y, abs_z)
    raw = item.get("waypoint_raw")
    return (
        item.get("item_id") or "",
        item.get("collection") or "",
        item.get("author") or "",
        abs_x, abs_y, abs_z,
        *_block_unit(item),
        cell[0], cell[1], cell[2],
        item.get("pitch"), item.get("yaw"), item.get("roll"),
        item.get("scale"),
        item.get("pivot_x"), item.get("pivot_y"), item.get("pivot_z"),
        item.get("flags"),
        item.get("waypoint_tag"),
        item.get("waypoint_order"),
        json.dumps(raw) if raw is not None else None,
        index,
    )


def ingest_map_items(
    conn,
    map_id: int,
    items: Sequence[dict[str, Any]],
    *,
    parser_version: str,
    version: str,
    artifact_ids: Iterable[str] = (),
) -> tuple[int, int, int]:
    """Replace the item rows for one map. Returns (items, waypoints, mismatches).

    Idempotent by delete-then-insert within one transaction, because
    placement_index is only stable for a given parser version and a
    partial re-parse would otherwise leave a mixture of two runs.
    """
    src = json.dumps(sorted(artifact_ids))
    mismatches = 0
    rows = []
    for i, item in enumerate(items):
        r = _row(item, i)
        # Source BlockUnitCoord vs the cell derived from abs position,
        # counted only where the source is actually set. A real
        # disagreement means the grid calibration is drifting and must
        # not be masked by trusting one side; the expected rate is a few
        # percent, all of them one cell apart at a boundary.
        if r[6] is not None and r[9] is not None and (r[6], r[7], r[8]) != (r[9], r[10], r[11]):
            mismatches += 1
        rows.append((map_id, parser_version) + r + (version, src))

    with conn.cursor() as cur:
        cur.execute("DELETE FROM map_items WHERE map_id = %s", (map_id,))
        if rows:
            cur.executemany(
                """
                INSERT INTO map_items (
                    map_id, parser_version,
                    item_id, item_collection, item_author,
                    abs_x, abs_y, abs_z,
                    block_unit_x, block_unit_y, block_unit_z,
                    cell_x, cell_y, cell_z,
                    pitch, yaw, roll, scale,
                    pivot_x, pivot_y, pivot_z,
                    flags, waypoint_tag, waypoint_order, waypoint_raw,
                    placement_index, created_by_version, source_artifact_ids
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                """,
                rows,
            )
    conn.commit()
    waypoints = sum(1 for it in items if it.get("waypoint_tag"))
    return len(rows), waypoints, mismatches


# The auto-generated ground layer. Every map carries exactly 2,304
# `Grass` cells covering the full 48x48 ground. Flagged rather than
# dropped, but it is redundant for matching: ground-plane driving is
# already handled as TERRAIN_GROUND, which does not need a row per cell.
BASE_TERRAIN_NAMES = {"Grass", "Water", "Dirt", "Snow"}


def _baked_row(b: dict[str, Any]) -> tuple:
    name = b.get("name") or ""
    return (
        name,
        b.get("model_id"), b.get("model_collection"), b.get("model_author"),
        b.get("x"), b.get("y"), b.get("z"), b.get("dir"),
        b.get("abs_x"), b.get("abs_y"), b.get("abs_z"),
        b.get("flags"), b.get("variant"), b.get("sub_variant"),
        int(bool(b.get("is_ground"))), int(bool(b.get("is_clip"))),
        int(bool(b.get("is_free"))), int(bool(b.get("is_ghost"))),
        int(bool(b.get("is_pillar"))),
        b.get("waypoint_tag"), b.get("waypoint_order"),
        json.dumps(b["waypoint_raw"]) if b.get("waypoint_raw") else None,
        int(name in BASE_TERRAIN_NAMES),
        b.get("placement_index", 0),
    )


def ingest_baked_blocks(
    conn,
    map_id: int,
    baked: Sequence[dict[str, Any]],
    *,
    parser_version: str,
    version: str,
    artifact_ids: Iterable[str] = (),
    skip_base_terrain: bool = False,
) -> tuple[int, int]:
    """Replace baked-block rows for one map. Returns (rows, terrain_rows)."""
    src = json.dumps(sorted(artifact_ids))
    rows = []
    terrain = 0
    for b in baked:
        is_terrain = (b.get("name") or "") in BASE_TERRAIN_NAMES
        terrain += is_terrain
        if is_terrain and skip_base_terrain:
            continue
        rows.append((map_id, parser_version) + _baked_row(b) + (version, src))

    with conn.cursor() as cur:
        cur.execute("DELETE FROM map_baked_blocks WHERE map_id = %s", (map_id,))
        if rows:
            cur.executemany(
                """
                INSERT INTO map_baked_blocks (
                    map_id, parser_version, block_name,
                    model_id, model_collection, model_author,
                    x, y, z, direction, abs_x, abs_y, abs_z,
                    flags, variant, sub_variant,
                    is_ground, is_clip, is_free, is_ghost, is_pillar,
                    waypoint_tag, waypoint_order, waypoint_raw,
                    base_terrain, placement_index,
                    created_by_version, source_artifact_ids
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                """,
                rows,
            )
    conn.commit()
    return len(rows), terrain


def ingest_maps(
    conn,
    parser,
    map_rows: Sequence[tuple[int, str]],
    *,
    version: str,
    skip_base_terrain: bool = False,
) -> ItemIngestStats:
    """Ingest items AND baked blocks for ``[(map_id, artifact_path), ...]``.

    One dump call feeds two tables. The rows stay separate because the
    collections mean different things: an item is author-placed with a
    free transform, a baked block is game-generated on the grid.
    """
    stats = ItemIngestStats()
    for map_id, artifact_path in map_rows:
        result = parser.dump_items(Path(artifact_path))
        if result.status != "success" or not result.output:
            _LOG.error(
                "map %s: dump-items failed (%s)", map_id,
                getattr(result, "error_code", "unknown"),
            )
            stats.failed_maps += 1
            stats.failures.append(str(map_id))
            continue
        items = result.output.get("items") or []
        n, wp, mism = ingest_map_items(
            conn, map_id, items,
            parser_version=parser.parser_version,
            version=version,
            artifact_ids=[artifact_path],
        )
        nb, nt = ingest_baked_blocks(
            conn, map_id, result.output.get("baked") or [],
            parser_version=parser.parser_version,
            version=version,
            artifact_ids=[artifact_path],
            skip_base_terrain=skip_base_terrain,
        )
        stats.maps += 1
        stats.items += n
        stats.waypoint_items += wp
        stats.unit_coord_mismatches += mism
        stats.baked += nb
        stats.baked_terrain += nt

    _LOG.info(
        "items: %d rows across %d maps (%d waypoint items, %d unit-coord "
        "mismatches, %d maps failed)",
        stats.items, stats.maps, stats.waypoint_items,
        stats.unit_coord_mismatches, stats.failed_maps,
    )
    _LOG.info(
        "baked blocks: %d rows stored (%d were base terrain)",
        stats.baked, stats.baked_terrain,
    )
    return stats
