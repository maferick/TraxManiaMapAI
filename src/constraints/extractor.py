"""Adjacency extraction from a single map's block placements.

A block at grid cell ``(x, y, z)`` is *spatially adjacent* to any
block whose cell shares one of the six axis-aligned faces:
``(x±1, y, z)``, ``(x, y±1, z)``, or ``(x, y, z±1)``. Diagonals and
edge-touches are intentionally excluded — a diagonal touch is
unusual enough in TM2020 geometry that folding it in would pollute
evidence counts without adding information.

The extractor is pure: no DB reads, no I/O. Pair order is
lexicographic so the caller can stream observations straight into
Neo4j MERGE without worrying about direction ambiguity.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from src.constraints.nodes import AdjacencyObservation, BlockKey, order_pair
from src.schema.maps import BlockPlacement

_AXIS_NEIGHBOR_OFFSETS: tuple[tuple[int, int, int], ...] = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 1, 0),
    (0, -1, 0),
    (0, 0, 1),
    (0, 0, -1),
)


def _block_key_from_placement(p: BlockPlacement) -> BlockKey:
    return BlockKey(
        family=p.block_family,
        type=p.block_type,
        variant=p.variant or "",
    )


def extract_adjacencies(
    placements: Sequence[BlockPlacement],
    *,
    snapshot_id: str,
    is_benchmark_strong: bool = False,
    is_broken_fixture: bool = False,
) -> list[AdjacencyObservation]:
    """Emit one :class:`AdjacencyObservation` per unordered adjacent pair
    within a single map.

    Duplicate unordered pairs (two blocks of the same family+type+variant
    adjacent to a third via different cells) collapse to a single
    observation — evidence counts are per *distinct adjacency*, not per
    physical placement pair.
    """
    by_cell: dict[tuple[int, int, int], BlockKey] = {}
    for p in placements:
        # Free blocks have no grid cell — they're positioned by world
        # coordinate and don't participate in axis-neighbor adjacency.
        # They can still contribute to a future directed-transition
        # graph, but that's a different edge type.
        if p.is_free or p.x is None or p.y is None or p.z is None:
            continue
        cell = (p.x, p.y, p.z)
        # Scaffold policy: first block wins for a given cell. Revisit
        # if real TM2020 data shows multi-block cells are common.
        by_cell.setdefault(cell, _block_key_from_placement(p))

    seen_pairs: set[tuple[str, str]] = set()
    out: list[AdjacencyObservation] = []
    for cell, key in by_cell.items():
        for dx, dy, dz in _AXIS_NEIGHBOR_OFFSETS:
            neighbor_cell = (cell[0] + dx, cell[1] + dy, cell[2] + dz)
            neighbor_key = by_cell.get(neighbor_cell)
            if neighbor_key is None:
                continue
            if neighbor_key == key:
                # Same block type meeting itself at an adjacent cell is a
                # valid observation: self-adjacency still carries info.
                pass
            a, b = order_pair(key, neighbor_key)
            pair_id = (a.normalized_key, b.normalized_key)
            if pair_id in seen_pairs:
                continue
            seen_pairs.add(pair_id)
            out.append(
                AdjacencyObservation(
                    a=a,
                    b=b,
                    snapshot_id=snapshot_id,
                    is_benchmark_strong=is_benchmark_strong,
                    is_broken_fixture=is_broken_fixture,
                )
            )
    return out


def unique_block_keys(
    observations: Iterable[AdjacencyObservation],
) -> list[BlockKey]:
    """Helper: the unique node set implied by a batch of observations.

    Useful for the pipeline's ``MERGE (:Block)`` pre-pass so per-edge
    MERGE queries never stall on node creation contention.
    """
    seen: dict[str, BlockKey] = {}
    for obs in observations:
        seen.setdefault(obs.a.normalized_key, obs.a)
        seen.setdefault(obs.b.normalized_key, obs.b)
    return list(seen.values())
