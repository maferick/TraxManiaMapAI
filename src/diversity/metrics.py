"""Diversity metrics for corridor rankings.

Canonical similarity: Jaccard on path cells. Everything else is a
composition on top.

Pure functions (aside from ``fetch_paths`` which talks to the DB).
Tests live in ``tests/unit/test_diversity_metrics.py``.
"""
from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from pymysql.connections import Connection

from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


Cell = tuple[int, int, int]


@dataclass(frozen=True)
class CorridorPath:
    """One corridor as consumed by the diversity metrics.

    We deliberately avoid pulling learned-ranking signals into this
    object — the similarity metric should be computable on raw
    geometry alone. Scores are stored separately for the cross-ranker
    comparison."""
    corridor_id: int
    map_id: int
    src_tag: str
    src_order: int
    dst_tag: str
    dst_order: int
    path_rank: int
    cells: frozenset[Cell]
    path_length: int
    contains_virtual_edge: bool
    corridor_confidence: float | None
    learned_score: float | None


@dataclass(frozen=True)
class IntervalDiversity:
    map_id: int
    src_tag: str
    src_order: int
    dst_tag: str
    dst_order: int
    corridor_count: int
    top_k: int
    mean_pairwise_similarity: float   # in [0, 1]; higher = more collapse
    diversity: float                  # 1 - mean_pairwise_similarity


@dataclass(frozen=True)
class RankerDiversitySummary:
    """Top-K-selected diversity stats for a single ranker.

    Produced once for the heuristic (``corridor_confidence``) and
    once for the learned model (``learned_corridor_score``) so a
    reader can compare them directly."""
    ranker: str                              # "heuristic" | "learned"
    intervals_compared: int
    mean_pairwise_similarity_median: float
    mean_pairwise_similarity_mean: float
    diversity_median: float
    diversity_mean: float
    worst_intervals: list[IntervalDiversity] = field(default_factory=list)


@dataclass(frozen=True)
class DiversityReport:
    total_corridors: int
    corridor_owning_maps: int
    top_k: int
    intervals: list[IntervalDiversity]       # using native path_rank ordering
    rank0_cross_map_similarity_quartiles: dict[str, float]
    virtual_edge_fraction_top_rank: float
    path_length_stdev_top_rank: float
    path_length_median_top_rank: float
    # Set when both scores are present on enough intervals.
    heuristic_summary: RankerDiversitySummary | None = None
    learned_summary: RankerDiversitySummary | None = None


# ---------------------------------------------------------------------
# Primitives — pure, no DB
# ---------------------------------------------------------------------

def jaccard(a: frozenset[Cell], b: frozenset[Cell]) -> float:
    """Jaccard similarity in ``[0, 1]``. Two empty paths are
    convention-defined to have similarity 0 (no shared geometry,
    not a "match")."""
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return inter / union


def top_k_pairwise_mean_similarity(
    paths: Sequence[CorridorPath],
    *,
    k: int,
) -> float:
    """Mean pairwise Jaccard over the first ``k`` entries of ``paths``.

    Caller is responsible for ordering ``paths`` by whatever rule they
    want (path_rank, corridor_confidence, learned_score, …).

    Returns ``0.0`` when < 2 eligible paths (no pairs to compare)."""
    if k < 2:
        raise ValueError("k must be >= 2 for pairwise similarity")
    if len(paths) < 2:
        return 0.0
    head = paths[:k]
    sims: list[float] = []
    for i in range(len(head)):
        for j in range(i + 1, len(head)):
            sims.append(jaccard(head[i].cells, head[j].cells))
    if not sims:
        return 0.0
    return sum(sims) / len(sims)


def compute_interval_diversity(
    paths: Sequence[CorridorPath], *, k: int,
) -> IntervalDiversity | None:
    """Compute per-interval mean pairwise similarity using the
    path_rank-native ordering. Returns ``None`` when the interval
    has < 2 corridors."""
    if not paths:
        return None
    if len(paths) < 2:
        return None
    ordered = sorted(paths, key=lambda p: p.path_rank)
    pivot = ordered[0]
    mean_sim = top_k_pairwise_mean_similarity(ordered, k=k)
    return IntervalDiversity(
        map_id=pivot.map_id,
        src_tag=pivot.src_tag,
        src_order=pivot.src_order,
        dst_tag=pivot.dst_tag,
        dst_order=pivot.dst_order,
        corridor_count=len(paths),
        top_k=min(k, len(paths)),
        mean_pairwise_similarity=mean_sim,
        diversity=1.0 - mean_sim,
    )


def _ranker_summary(
    ranker: str,
    intervals_by_key: dict[tuple[int, str, int, str, int], list[CorridorPath]],
    *,
    k: int,
    score_key: str,             # "corridor_confidence" | "learned_score"
    worst_n: int = 5,
) -> RankerDiversitySummary | None:
    """Re-rank each interval by the ranker's score (descending), then
    compute top-K pairwise similarity. Intervals where the ranker
    can't score everything are skipped — a NaN drops the interval
    rather than silently biasing the mean."""
    collected: list[IntervalDiversity] = []
    for paths in intervals_by_key.values():
        if len(paths) < 2:
            continue
        scored = [(getattr(p, score_key), p) for p in paths]
        if any(s is None for s, _ in scored):
            continue
        scored.sort(key=lambda sp: sp[0], reverse=True)
        ordered = [p for _, p in scored]
        if len(ordered) < 2:
            continue
        mean_sim = top_k_pairwise_mean_similarity(ordered, k=k)
        pivot = ordered[0]
        collected.append(IntervalDiversity(
            map_id=pivot.map_id,
            src_tag=pivot.src_tag,
            src_order=pivot.src_order,
            dst_tag=pivot.dst_tag,
            dst_order=pivot.dst_order,
            corridor_count=len(paths),
            top_k=min(k, len(paths)),
            mean_pairwise_similarity=mean_sim,
            diversity=1.0 - mean_sim,
        ))
    if not collected:
        return None
    sims = [c.mean_pairwise_similarity for c in collected]
    divs = [c.diversity for c in collected]
    # Worst = highest similarity (= lowest diversity).
    worst = sorted(
        collected, key=lambda c: c.mean_pairwise_similarity, reverse=True,
    )[:worst_n]
    return RankerDiversitySummary(
        ranker=ranker,
        intervals_compared=len(collected),
        mean_pairwise_similarity_median=float(statistics.median(sims)),
        mean_pairwise_similarity_mean=float(statistics.mean(sims)),
        diversity_median=float(statistics.median(divs)),
        diversity_mean=float(statistics.mean(divs)),
        worst_intervals=worst,
    )


def _rank0_cross_map_similarity_quartiles(
    paths: Sequence[CorridorPath], *, sample_cap: int = 2000,
) -> dict[str, float]:
    """Cross-map pairwise Jaccard distribution across all rank-0
    corridors. Quartiles only — the full pairwise set is
    ``O(N²)``, so we cap at a random sample of pairs (seed-fixed)
    when ``N > sqrt(sample_cap * 2)``."""
    import random
    rank0 = [p for p in paths if p.path_rank == 0]
    if len(rank0) < 2:
        return {"n_pairs": 0.0, "q1": 0.0, "median": 0.0, "q3": 0.0, "mean": 0.0}
    pairs: list[tuple[CorridorPath, CorridorPath]] = []
    max_pairs = int(sample_cap)
    rng = random.Random(42)
    n = len(rank0)
    if n * (n - 1) // 2 <= max_pairs:
        pairs = [(rank0[i], rank0[j]) for i in range(n) for j in range(i + 1, n)]
    else:
        seen: set[tuple[int, int]] = set()
        while len(pairs) < max_pairs:
            i = rng.randrange(n)
            j = rng.randrange(n)
            if i == j:
                continue
            key = (min(i, j), max(i, j))
            if key in seen:
                continue
            seen.add(key)
            pairs.append((rank0[key[0]], rank0[key[1]]))
    sims = [jaccard(a.cells, b.cells) for a, b in pairs]
    sims.sort()
    def _q(p: float) -> float:
        if not sims:
            return 0.0
        k = max(0, min(len(sims) - 1, int(p * (len(sims) - 1))))
        return float(sims[k])
    return {
        "n_pairs": float(len(sims)),
        "q1": _q(0.25),
        "median": _q(0.5),
        "q3": _q(0.75),
        "mean": float(sum(sims) / len(sims)) if sims else 0.0,
    }


def build_report(
    paths: Sequence[CorridorPath], *, k: int = 3,
) -> DiversityReport:
    """Assemble a :class:`DiversityReport` from a collection of
    materialized paths."""
    # Group by interval.
    by_interval: dict[tuple[int, str, int, str, int], list[CorridorPath]] = {}
    for p in paths:
        by_interval.setdefault(
            (p.map_id, p.src_tag, p.src_order, p.dst_tag, p.dst_order), [],
        ).append(p)

    interval_diversities: list[IntervalDiversity] = []
    for key, interval_paths in by_interval.items():
        d = compute_interval_diversity(interval_paths, k=k)
        if d is not None:
            interval_diversities.append(d)

    cross_quartiles = _rank0_cross_map_similarity_quartiles(paths)

    rank0 = [p for p in paths if p.path_rank == 0]
    if rank0:
        virtual_frac = sum(1 for p in rank0 if p.contains_virtual_edge) / len(rank0)
        lengths = [p.path_length for p in rank0]
        path_length_stdev = (
            float(statistics.stdev(lengths)) if len(lengths) >= 2 else 0.0
        )
        path_length_median = float(statistics.median(lengths))
    else:
        virtual_frac = 0.0
        path_length_stdev = 0.0
        path_length_median = 0.0

    # Cross-ranker summaries where scored.
    heuristic_summary = _ranker_summary(
        "heuristic", by_interval, k=k, score_key="corridor_confidence",
    )
    learned_summary = _ranker_summary(
        "learned", by_interval, k=k, score_key="learned_score",
    )

    return DiversityReport(
        total_corridors=len(paths),
        corridor_owning_maps=len({p.map_id for p in paths}),
        top_k=k,
        intervals=interval_diversities,
        rank0_cross_map_similarity_quartiles=cross_quartiles,
        virtual_edge_fraction_top_rank=virtual_frac,
        path_length_stdev_top_rank=path_length_stdev,
        path_length_median_top_rank=path_length_median,
        heuristic_summary=heuristic_summary,
        learned_summary=learned_summary,
    )


# ---------------------------------------------------------------------
# DB collection
# ---------------------------------------------------------------------

def fetch_paths(
    conn: Connection,
    *,
    snapshot_id: str | None = None,
) -> list[CorridorPath]:
    """Materialize CorridorPath objects from ``route_corridors``.
    Parses ``path_cells`` JSON into frozensets for efficient Jaccard.

    ``snapshot_id`` filters via the parent map's ingestion snapshot.
    """
    if snapshot_id is None:
        sql = """
            SELECT id, map_id, src_tag, src_order, dst_tag, dst_order,
                   path_rank, path_cells, path_length, contains_virtual_edge,
                   corridor_confidence, learned_corridor_score
            FROM route_corridors
        """
        params: tuple = ()
    else:
        sql = """
            SELECT rc.id, rc.map_id, rc.src_tag, rc.src_order, rc.dst_tag,
                   rc.dst_order, rc.path_rank, rc.path_cells, rc.path_length,
                   rc.contains_virtual_edge, rc.corridor_confidence,
                   rc.learned_corridor_score
            FROM route_corridors rc
            JOIN maps m ON m.id = rc.map_id
            WHERE m.ingestion_snapshot = %s
        """
        params = (snapshot_id,)

    with cursor(conn) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    out: list[CorridorPath] = []
    for r in rows:
        try:
            cells_list = json.loads(r[7])
        except (TypeError, json.JSONDecodeError):
            continue
        cells = frozenset(tuple(c) for c in cells_list if len(c) == 3)
        out.append(CorridorPath(
            corridor_id=int(r[0]),
            map_id=int(r[1]),
            src_tag=str(r[2]),
            src_order=int(r[3]),
            dst_tag=str(r[4]),
            dst_order=int(r[5]),
            path_rank=int(r[6]),
            cells=cells,
            path_length=int(r[8]),
            contains_virtual_edge=bool(r[9]),
            corridor_confidence=(float(r[10]) if r[10] is not None else None),
            learned_score=(float(r[11]) if r[11] is not None else None),
        ))
    return out
