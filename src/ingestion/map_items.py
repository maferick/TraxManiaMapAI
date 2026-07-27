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


def _row(item: dict[str, Any], index: int) -> tuple:
    abs_x, abs_y, abs_z = item.get("abs_x"), item.get("abs_y"), item.get("abs_z")
    cell = derive_cell(abs_x, abs_y, abs_z)
    raw = item.get("waypoint_raw")
    return (
        item.get("item_id") or "",
        item.get("collection") or "",
        item.get("author") or "",
        abs_x, abs_y, abs_z,
        item.get("cell_x"), item.get("cell_y"), item.get("cell_z"),
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
        # Source BlockUnitCoord vs the cell derived from abs position.
        # These agreed exactly on the item checked by hand, so a
        # disagreement is a calibration signal worth surfacing rather
        # than silently trusting one side.
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


def ingest_maps(
    conn,
    parser,
    map_rows: Sequence[tuple[int, str]],
    *,
    version: str,
) -> ItemIngestStats:
    """Ingest items for ``[(map_id, artifact_path), ...]``."""
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
        stats.maps += 1
        stats.items += n
        stats.waypoint_items += wp
        stats.unit_coord_mismatches += mism

    _LOG.info(
        "items: %d rows across %d maps (%d waypoint items, %d unit-coord "
        "mismatches, %d maps failed)",
        stats.items, stats.maps, stats.waypoint_items,
        stats.unit_coord_mismatches, stats.failed_maps,
    )
    return stats
