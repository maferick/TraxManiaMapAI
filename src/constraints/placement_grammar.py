"""Mine the corpus for how mappers actually place blocks next to blocks.

This replaces clip-derivation as the source of truth for what the
generator may build.

``face_transitions`` counted only pairs whose route-clips matched on
touching faces. That encodes one way to build a track, and inspecting
real maps showed it excludes things mappers do constantly. Corpus map
25192 (a plastic map) alone breaks four of the walker's rules:

* ``GateCheckpoint`` / ``GateFinish`` / ``GateSpecial*`` carry **no
  clips at all** and are 1x4x1 — they are arches placed *over* the
  route, not links in it
* ``GateExpandableFinish`` appears 15 times as a tiled 3x5 wall of
  1x1x1 cells, so a "gate" can be an assembly
* one row runs ``PlatformTechStart`` -> ``PlatformWaterSpecialTurbo2``
  -> ``PlatformTechToDecoWall``: three different surfaces in a line
* ``PlatformTechToDecoWall`` and ``PlatformWaterSpecialTurbo2`` share
  the cell (25, 10, 20) — two blocks, one cell

So validity here is **evidence**, not derivation: a pair is legal
because N distinct maps contain it. Clip agreement is recorded as a
signal (clip-matched pairs are safer bets) but never as a filter.

Offsets are stored in block A's own rotation frame, so a pattern
learned facing north applies at every heading.

The scan is vectorised because it has to be: 18,935 Stadium2020 maps
hold ~65M grid placements, and at radius 3 each one has ~55 neighbours
— 3.6 billion pairs. Per map the whole neighbour join is a handful of
``searchsorted`` calls over the map's own cell keys, and counts are
merged in sorted numpy arrays rather than a Python dict, which would
need tens of GB for the key space.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
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

STAGE_VERSION = "placement-grammar-v1"

# How far apart two blocks may be and still count as related.
#
# 3 cells in XZ spans the gap jumps real maps use. Y is tighter: a
# continuation or an overlay is within a couple of levels, while a
# block 3 levels up is a different part of the map that happens to be
# overhead. Every extra ring costs quadratically and lands in the
# noise tail, which ``min_map_count`` would drop anyway.
DEFAULT_RADIUS_XZ = 3
DEFAULT_RADIUS_Y = 2

# Bit budget for the packed grammar key (see ``_pack_keys``). 17 bits
# per block index covers 131k distinct types; the corpus has ~8k
# (3893 Nadeo Stadium2020 + ~4200 community).
_TYPE_BITS = 17
_MAX_TYPES = 1 << _TYPE_BITS

# Merge the running totals once this many per-map keys are pending.
# Bounds peak memory during the fold.
_COMPACT_EVERY = 4_000_000

# A full scan is ~40 minutes of per-map queries, and the corpus host
# is shared — MariaDB has been OOM-killed under load there before.
# Losing the whole scan to a server restart is not acceptable, so
# reconnect and retry rather than dying on a broken pipe.
_DB_RETRIES = 6
_DB_BACKOFF = 5.0


@dataclass
class GrammarReport:
    maps_seen: int = 0
    placements_seen: int = 0
    pairs_counted: int = 0
    distinct_keys: int = 0
    rows_written: int = 0
    clip_matched_rows: int = 0
    gap_rows: int = 0
    overlay_rows: int = 0
    errors: list[str] = field(default_factory=list)


_SCAN_MAPS_SQL = """
SELECT id
FROM maps
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
INSERT INTO block_placement_grammar (
    pair_signature, block_a, block_b, dx, dy, dz, rel_rotation,
    environment, clip_matched, pair_count, map_count, created_by_version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    pair_count = pair_count + VALUES(pair_count),
    map_count  = map_count  + VALUES(map_count),
    updated_at = CURRENT_TIMESTAMP(6),
    created_by_version = VALUES(created_by_version)
"""


def reset_grammar(conn: Connection) -> None:
    with cursor(conn) as cur:
        cur.execute("TRUNCATE TABLE block_placement_grammar")
    conn.commit()


def _pair_signature(
    block_a: str, block_b: str, dx: int, dy: int, dz: int,
    rel_rotation: int, environment: str,
) -> str:
    payload = "|".join((
        block_a, block_b, str(dx), str(dy), str(dz),
        str(rel_rotation), environment,
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _with_reconnect(
    conn: Connection,
    reconnect: Callable[[], Connection] | None,
    run: Callable[[Connection], object],
) -> tuple[Connection, object]:
    """Run a query, surviving a server restart underneath it.

    Returns the connection to keep using — it may be a new one.
    """
    last: Exception | None = None
    for attempt in range(_DB_RETRIES):
        try:
            return conn, run(conn)
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as exc:
            last = exc
            if reconnect is None or attempt == _DB_RETRIES - 1:
                break
            wait = _DB_BACKOFF * (attempt + 1)
            _LOG.warning(
                "%s: database error (%s); reconnecting in %.0fs "
                "(attempt %d/%d)",
                STAGE_VERSION, exc, wait, attempt + 1, _DB_RETRIES,
            )
            time.sleep(wait)
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - already broken
                pass
            conn = reconnect()
    raise RuntimeError(f"database unavailable after {_DB_RETRIES} attempts") from last


class _OffsetTable:
    """The neighbourhood, plus how a world offset maps into A's frame."""

    def __init__(self, radius_xz: int, radius_y: int) -> None:
        offsets: list[tuple[int, int, int]] = []
        for dx in range(-radius_xz, radius_xz + 1):
            for dy in range(-radius_y, radius_y + 1):
                for dz in range(-radius_xz, radius_xz + 1):
                    offsets.append((dx, dy, dz))
        self.offsets = offsets
        index = {o: i for i, o in enumerate(offsets)}
        # rot_map[a_rot][world_offset_index] -> index of the same
        # displacement expressed in a block-A-at-rotation-a_rot frame.
        # The box is square in XZ, so a rotated offset stays inside it.
        self.rot_map = np.empty((4, len(offsets)), dtype=np.int64)
        for a_rot in range(4):
            for i, off in enumerate(offsets):
                local = rotate_vector(off, (4 - a_rot) % 4)
                self.rot_map[a_rot, i] = index[local]

    def __len__(self) -> int:
        return len(self.offsets)


def _clip_port_index(
    catalogue: dict[str, BlockDef],
) -> dict[tuple[str, int], list[tuple[tuple[int, int, int], int, str]]]:
    """(block, rotation) -> [(cell offset, world face, clip id)]."""
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


def _clips_meet(
    ports: dict[tuple[str, int], list],
    block_a: str, block_b: str,
    dx: int, dy: int, dz: int, rel_rotation: int,
) -> bool:
    """Would a route-clip meet for this key?

    Rotation-invariant, because the stored offset is already in A's
    frame: place A at rotation 0 and B at ``rel_rotation``, then ask
    the same question ``face_transitions`` asks. Evaluated once per
    distinct key rather than once per observed pair — the difference
    between 250k calls and 3.6 billion.
    """
    a_ports = ports.get((block_a, 0))
    b_ports = ports.get((block_b, rel_rotation))
    if not a_ports or not b_ports:
        return False
    b_cells = {
        (dx + c[0], dy + c[1], dz + c[2], face): clip
        for c, face, clip in b_ports
    }
    for cell, face, clip in a_ports:
        fdx, fdy, fdz = FACE_DELTAS[face]
        other = b_cells.get(
            (cell[0] + fdx, cell[1] + fdy, cell[2] + fdz, opposite_face(face))
        )
        if other is not None and other == clip:
            return True
    return False


class _Counter:
    """Sorted-array counter for a key space too big for a dict.

    Per-map key arrays are appended and periodically folded into the
    running totals. ``map_count`` works because each map contributes
    each key exactly once.

    The fold is written to keep its peak down rather than for brevity.
    ``np.unique(..., return_inverse=True)`` plus two weighted
    ``bincount`` calls is the obvious version and allocates an int64
    inverse the length of the whole input plus two float64 outputs the
    length of the result; measured against the real corpus that peaked
    at 10.5 GB and would have been OOM-killed before the end. Sorting
    once and summing runs with ``reduceat`` costs about a third of
    that. ``maps`` is int32 because the corpus is 18,935 maps.
    """

    def __init__(self) -> None:
        self.keys = np.empty(0, dtype=np.int64)
        self.pairs = np.empty(0, dtype=np.int64)
        self.maps = np.empty(0, dtype=np.int32)
        self._pending_keys: list[np.ndarray] = []
        self._pending_pairs: list[np.ndarray] = []
        self._pending_size = 0

    def add(self, keys: np.ndarray, counts: np.ndarray) -> None:
        if keys.size == 0:
            return
        self._pending_keys.append(keys)
        self._pending_pairs.append(counts.astype(np.int64, copy=False))
        self._pending_size += int(keys.size)
        if self._pending_size >= _COMPACT_EVERY:
            self.compact()

    def compact(self) -> None:
        if not self._pending_keys:
            return
        keys = np.concatenate([self.keys, *self._pending_keys])
        pairs = np.concatenate([self.pairs, *self._pending_pairs])
        maps = np.concatenate(
            [self.maps]
            + [np.ones(k.size, dtype=np.int32) for k in self._pending_keys]
        )
        self._pending_keys.clear()
        self._pending_pairs.clear()
        self._pending_size = 0

        order = np.argsort(keys, kind="stable")
        keys = keys[order]
        pairs = pairs[order]
        maps = maps[order]
        del order

        boundary = np.empty(keys.size, dtype=bool)
        boundary[0] = True
        np.not_equal(keys[1:], keys[:-1], out=boundary[1:])
        starts = np.flatnonzero(boundary)
        del boundary

        self.keys = keys[starts]
        self.pairs = np.add.reduceat(pairs, starts)
        self.maps = np.add.reduceat(maps, starts)


def _map_pairs(
    type_idx: np.ndarray,
    rot: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    table: _OffsetTable,
) -> tuple[np.ndarray, np.ndarray]:
    """All neighbour pairs in one map, as unique packed keys + counts.

    One sort-merge join per offset. Cells can hold more than one block
    (real maps stack them), so the join expands ranges rather than
    assuming a cell maps to a single placement.
    """
    n = type_idx.size
    # Pack a cell into one sortable int64. The bias keeps every field
    # non-negative, so a neighbour lookup stays inside its own field
    # even at negative coordinates (maps do use them).
    bias = 1 << 19
    cell = (
        ((x.astype(np.int64) + bias) << 40)
        + ((y.astype(np.int64) + bias) << 20)
        + (z.astype(np.int64) + bias)
    )
    order = np.argsort(cell, kind="stable")
    sorted_cells = cell[order]

    n_off = len(table)
    rot_map = table.rot_map
    all_keys: list[np.ndarray] = []

    for oi, (dx, dy, dz) in enumerate(table.offsets):
        # Addition, not bitwise OR: a negative component would flood
        # every lower bit and match unrelated cells.
        shift = (
            (int(dx) << 40) + (int(dy) << 20) + int(dz)
        )
        target = cell + shift
        lo = np.searchsorted(sorted_cells, target, side="left")
        hi = np.searchsorted(sorted_cells, target, side="right")
        counts = hi - lo
        total = int(counts.sum())
        if total == 0:
            continue
        a_idx = np.repeat(np.arange(n, dtype=np.int64), counts)
        # Expand each [lo, hi) range without a Python loop.
        starts = np.repeat(lo.astype(np.int64), counts)
        ranks = np.arange(total, dtype=np.int64) - np.repeat(
            np.cumsum(counts, dtype=np.int64) - counts, counts
        )
        b_idx = order[starts + ranks]
        if dx == 0 and dy == 0 and dz == 0:
            keep = a_idx != b_idx
            a_idx, b_idx = a_idx[keep], b_idx[keep]
            if a_idx.size == 0:
                continue

        a_rot = rot[a_idx].astype(np.int64)
        rel_rot = (rot[b_idx].astype(np.int64) - a_rot) & 3
        stored_oi = rot_map[a_rot, oi]
        all_keys.append(
            _pack_keys(
                type_idx[a_idx], type_idx[b_idx], rel_rot, stored_oi, n_off
            )
        )

    if not all_keys:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    keys = np.concatenate(all_keys)
    return np.unique(keys, return_counts=True)


def _pack_keys(
    ta: np.ndarray, tb: np.ndarray, rel_rot: np.ndarray,
    stored_oi: np.ndarray, n_off: int,
) -> np.ndarray:
    return (
        ((ta.astype(np.int64) << _TYPE_BITS | tb.astype(np.int64)) * 4
         + rel_rot) * n_off + stored_oi
    )


def _unpack_key(key: int, n_off: int) -> tuple[int, int, int, int]:
    stored_oi = key % n_off
    rest = key // n_off
    rel_rot = rest & 3
    rest >>= 2
    tb = rest & (_MAX_TYPES - 1)
    ta = rest >> _TYPE_BITS
    return int(ta), int(tb), int(rel_rot), int(stored_oi)


def build_grammar(
    conn: Connection,
    catalogue: dict[str, BlockDef],
    environment: str = "Stadium2020",
    limit: int | None = None,
    radius_xz: int = DEFAULT_RADIUS_XZ,
    radius_y: int = DEFAULT_RADIUS_Y,
    min_map_count: int = 3,
    reconnect: Callable[[], Connection] | None = None,
) -> GrammarReport:
    """Count observed (A, B, offset in A's frame, relative rotation).

    One environment per run: the corpus spans six TM games and their
    geometry is not interchangeable, and keeping the key space to one
    environment is also what makes the packing fit.

    ``min_map_count`` drops pairs seen in fewer than N maps before
    writing. One mapper's quirk is not a grammar rule, and the
    single-map tail is most of the key space.

    Takes ownership of ``conn``: a reconnect replaces the object, so
    the caller's reference goes stale and must not be closed.
    """
    report = GrammarReport()
    table = _OffsetTable(radius_xz, radius_y)
    ports = _clip_port_index(catalogue)

    limit_clause = f"LIMIT {int(limit)}" if limit else ""

    def _scan(c: Connection):
        with cursor(c) as cur:
            cur.execute(
                _SCAN_MAPS_SQL.format(limit_clause=limit_clause), (environment,)
            )
            return [int(r[0]) for r in cur.fetchall()]

    conn, map_ids = _with_reconnect(conn, reconnect, _scan)

    _LOG.info(
        "%s: %d maps in %s, %d offsets (xz=%d, y=%d)",
        STAGE_VERSION, len(map_ids), environment, len(table),
        radius_xz, radius_y,
    )

    type_ids: dict[str, int] = {}
    type_names: list[str] = []
    counter = _Counter()

    for map_id in map_ids:
        report.maps_seen += 1
        if report.maps_seen % 500 == 0:
            # Distinct keys is the number that decides whether this
            # run fits in memory, so log it rather than infer it.
            _LOG.info(
                "%s: %d/%d maps, %d placements, %d pairs, %d keys",
                STAGE_VERSION, report.maps_seen, len(map_ids),
                report.placements_seen, report.pairs_counted,
                counter.keys.size,
            )
        def _load(c: Connection, _id: int = map_id):
            with cursor(c) as cur:
                cur.execute(_PLACEMENTS_SQL, (_id,))
                return cur.fetchall()

        conn, rows = _with_reconnect(conn, reconnect, _load)
        if not rows:
            continue
        report.placements_seen += len(rows)

        idx = np.empty(len(rows), dtype=np.int64)
        rot = np.empty(len(rows), dtype=np.int64)
        xs = np.empty(len(rows), dtype=np.int64)
        ys = np.empty(len(rows), dtype=np.int64)
        zs = np.empty(len(rows), dtype=np.int64)
        for i, (block_type, rotation, bx, by, bz) in enumerate(rows):
            name = str(block_type)
            tid = type_ids.get(name)
            if tid is None:
                tid = len(type_names)
                if tid >= _MAX_TYPES:
                    raise RuntimeError(
                        f"more than {_MAX_TYPES} block types; widen _TYPE_BITS"
                    )
                type_ids[name] = tid
                type_names.append(name)
            idx[i] = tid
            rot[i] = int(rotation) % 4
            xs[i], ys[i], zs[i] = int(bx), int(by), int(bz)

        keys, counts = _map_pairs(idx, rot, xs, ys, zs, table)
        report.pairs_counted += int(counts.sum())
        counter.add(keys, counts)

    counter.compact()
    report.distinct_keys = int(counter.keys.size)
    _LOG.info(
        "%s: scan done — %d placements, %d pairs, %d distinct keys",
        STAGE_VERSION, report.placements_seen, report.pairs_counted,
        report.distinct_keys,
    )

    keep = counter.maps >= min_map_count
    keys = counter.keys[keep]
    pair_counts = counter.pairs[keep]
    map_counts = counter.maps[keep]
    _LOG.info(
        "%s: %d/%d keys survive min_map_count=%d",
        STAGE_VERSION, keys.size, counter.keys.size, min_map_count,
    )

    # The counter's running arrays are the biggest thing in the
    # process; the surviving slice is all the write loop needs.
    del counter

    def _flush(c: Connection, rows: list[tuple]) -> None:
        with cursor(c) as cur:
            cur.executemany(_UPSERT_SQL, rows)
        c.commit()

    n_off = len(table)
    clip_cache: dict[tuple[int, int, int, int], bool] = {}
    batch: list[tuple] = []
    for key, pair_count, map_count in zip(
        keys.tolist(), pair_counts.tolist(), map_counts.tolist()
    ):
        ta, tb, rel_rot, stored_oi = _unpack_key(int(key), n_off)
        block_a, block_b = type_names[ta], type_names[tb]
        dx, dy, dz = table.offsets[stored_oi]

        cache_key = (ta, tb, rel_rot, stored_oi)
        matched = clip_cache.get(cache_key)
        if matched is None:
            matched = _clips_meet(
                ports, block_a, block_b, dx, dy, dz, rel_rot
            )
            clip_cache[cache_key] = matched

        if matched:
            report.clip_matched_rows += 1
        elif (dx, dy, dz) == (0, 0, 0):
            report.overlay_rows += 1
        elif max(abs(dx), abs(dz)) > 1:
            report.gap_rows += 1

        batch.append((
            _pair_signature(
                block_a, block_b, dx, dy, dz, rel_rot, environment
            ),
            block_a, block_b, dx, dy, dz, rel_rot, environment,
            int(matched), int(pair_count), int(map_count),
            STAGE_VERSION,
        ))
        report.rows_written += 1
        if len(batch) >= 5000:
            conn, _ = _with_reconnect(
                conn, reconnect, lambda c, rows=list(batch): _flush(c, rows)
            )
            batch.clear()
    if batch:
        conn, _ = _with_reconnect(
            conn, reconnect, lambda c, rows=list(batch): _flush(c, rows)
        )
    # This function owns the connection from here: reconnects have
    # replaced the object the caller handed in, so the caller cannot
    # close it.
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - already gone is fine
        pass

    _LOG.info(
        "%s: maps=%d placements=%d pairs=%d rows=%d "
        "(clip-matched=%d, overlay=%d, gap=%d)",
        STAGE_VERSION, report.maps_seen, report.placements_seen,
        report.pairs_counted, report.rows_written,
        report.clip_matched_rows, report.overlay_rows, report.gap_rows,
    )
    return report
