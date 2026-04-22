"""Feature extraction for corridor ranking.

One row per corridor. Features are chosen to be computable without
re-enumerating — everything comes from ``route_corridors`` +
``traversability_edge_evidence`` + ``block_placements`` for cell
resolution. Features that the current heuristic already uses are
included explicitly so the model can learn to re-weight them; new
features (interval size, path-length ratio, neighbor-pattern
uniformity) give the model room to discover signal the heuristic
doesn't capture.

Design constraint: features are bounded, interpretable, and
dimensionless where possible. No raw counts where a ratio is
defensible — the model shouldn't overfit to "this map is big."
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from pymysql.connections import Connection

from src.corridor.scoring import EdgeEvidence
from src.corridor.traversability.classification import CLASSIFICATION_VERSION
from src.corridor.traversability.evidence import (
    _cell_to_placement_map,
    _fetch_grid_placements,
)
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


# Ordered list of feature names. Same order everywhere the matrix is
# built so the model's learned weights can be reported interpretably.
FEATURE_NAMES: tuple[str, ...] = (
    "bias",                          # constant 1.0 — intercept term
    "path_length_log",               # log(1 + cell count)
    "contains_virtual_edge",         # 0 / 1
    "rule_support_fraction",         # fraction of edges with rule_support=1
    "mean_path_support_log",         # mean log(1+path_support) across edges
    "max_path_support_log",          # max log(1+path_support) across edges
    "mean_pattern_weight",           # mean pattern_weight across edges
    "mean_neg_evidence_frac",        # mean negative_evidence_count / 12
    "max_neg_evidence_frac",         # max negative_evidence_count / 12
    "interval_corridor_count_log",   # log(1 + N) where N = corridors in this interval
    # NOTE: path_rank / is_top_rank intentionally NOT included. The
    # label is derived from path_rank, so including it would leak
    # the answer into the features. See train.py for the honest
    # train/test discipline.
)


@dataclass(frozen=True)
class CorridorRow:
    """DB row + derived evidence-array, everything needed for feature
    extraction in one record."""
    corridor_id: int
    map_id: int
    src_tag: str
    src_order: int
    dst_tag: str
    dst_order: int
    path_rank: int
    path_cells: list[tuple[int, int, int]]
    path_length: int
    contains_virtual_edge: bool
    corridor_confidence: float | None    # existing heuristic output, for baseline comparison
    edge_evidences: list[EdgeEvidence]   # per grid-edge evidence (virtual-edge hops omitted)
    interval_corridor_count: int


@dataclass(frozen=True)
class CorridorFeatureVector:
    """Parallel to CorridorRow but with the numpy-ready feature row."""
    corridor_id: int
    map_id: int
    features: np.ndarray                 # shape (len(FEATURE_NAMES),)
    corridor_confidence: float | None    # None if unscored


def load_corridor_rows(
    conn: Connection,
    *,
    map_ids: Sequence[int] | None = None,
    classification_version: str = CLASSIFICATION_VERSION,
) -> list[CorridorRow]:
    """Materialize corridor rows joined with per-edge evidence. One
    DB round-trip per map (the evidence JOIN would require per-edge
    lookups if batched differently). Good enough for ~900 corridors
    across ~200 maps.
    """
    # 1. Which maps do we care about?
    with cursor(conn) as cur:
        if map_ids is None:
            cur.execute(
                "SELECT DISTINCT map_id FROM route_corridors "
                "WHERE classification_version = %s ORDER BY map_id",
                (classification_version,),
            )
            target_ids = [int(r[0]) for r in cur.fetchall()]
        else:
            target_ids = [int(m) for m in map_ids]

    out: list[CorridorRow] = []
    for map_id in target_ids:
        placements = _fetch_grid_placements(conn, map_id=map_id)
        if not placements:
            continue
        cell_to_pid = _cell_to_placement_map(placements)

        # Per-edge evidence indexed by (lo_pid, hi_pid).
        with cursor(conn) as cur:
            cur.execute(
                "SELECT src_block_id, dst_block_id, rule_support, "
                "path_support_count, pattern_weight, negative_evidence_count "
                "FROM traversability_edge_evidence "
                "WHERE map_id = %s AND classification_version = %s",
                (map_id, classification_version),
            )
            ev_rows = cur.fetchall()
        evidence_by_pair: dict[tuple[int, int], EdgeEvidence] = {
            (int(r[0]), int(r[1])): EdgeEvidence(
                rule_support=bool(r[2]),
                path_support_count=int(r[3]),
                pattern_weight=float(r[4]),
                negative_evidence_count=int(r[5]),
            )
            for r in ev_rows if int(r[0]) < int(r[1])
        }

        # Corridors on this map.
        with cursor(conn) as cur:
            cur.execute(
                "SELECT id, src_tag, src_order, dst_tag, dst_order, "
                "path_rank, path_cells, path_length, contains_virtual_edge, "
                "corridor_confidence "
                "FROM route_corridors "
                "WHERE map_id = %s AND classification_version = %s",
                (map_id, classification_version),
            )
            corridor_rows = cur.fetchall()

        # Count corridors per (interval) so we can derive
        # interval_corridor_count below.
        interval_counts: dict[tuple[str, int, str, int], int] = {}
        for r in corridor_rows:
            key = (r[1], int(r[2]), r[3], int(r[4]))
            interval_counts[key] = interval_counts.get(key, 0) + 1

        for r in corridor_rows:
            try:
                cells = [tuple(c) for c in json.loads(r[6])]
            except (TypeError, json.JSONDecodeError):
                continue
            interval_key = (r[1], int(r[2]), r[3], int(r[4]))
            # Walk consecutive cells, pull grid-edge evidence. Virtual
            # edges (cell pair not in evidence_by_pair) are skipped —
            # the contains_virtual_edge flag captures the fact.
            edge_evidences: list[EdgeEvidence] = []
            for i in range(len(cells) - 1):
                pid_a = cell_to_pid.get(cells[i])
                pid_b = cell_to_pid.get(cells[i + 1])
                if pid_a is None or pid_b is None:
                    continue
                lo, hi = (pid_a, pid_b) if pid_a < pid_b else (pid_b, pid_a)
                ev = evidence_by_pair.get((lo, hi))
                if ev is not None:
                    edge_evidences.append(ev)
            out.append(CorridorRow(
                corridor_id=int(r[0]),
                map_id=map_id,
                src_tag=str(r[1]),
                src_order=int(r[2]),
                dst_tag=str(r[3]),
                dst_order=int(r[4]),
                path_rank=int(r[5]),
                path_cells=cells,
                path_length=int(r[7]),
                contains_virtual_edge=bool(r[8]),
                corridor_confidence=(float(r[9]) if r[9] is not None else None),
                edge_evidences=edge_evidences,
                interval_corridor_count=interval_counts[interval_key],
            ))
    return out


def _featurize_one(row: CorridorRow) -> np.ndarray:
    """Return a single feature vector in FEATURE_NAMES order."""
    n_edges = len(row.edge_evidences)
    if n_edges > 0:
        rule_support_frac = sum(1 for e in row.edge_evidences if e.rule_support) / n_edges
        path_supports = [e.path_support_count for e in row.edge_evidences]
        pattern_weights = [e.pattern_weight for e in row.edge_evidences]
        neg_fracs = [e.negative_evidence_count / 12.0 for e in row.edge_evidences]
        mean_path_support_log = sum(math.log(1 + s) for s in path_supports) / n_edges
        max_path_support_log = math.log(1 + max(path_supports))
        mean_pattern_weight = sum(pattern_weights) / n_edges
        mean_neg_frac = sum(neg_fracs) / n_edges
        max_neg_frac = max(neg_fracs)
    else:
        # Zero-edge corridor (virtual-only spawn→goal hop). Neutral
        # values everywhere; rule_support_fraction defaults to 0
        # since "no edges" gives no positive evidence.
        rule_support_frac = 0.0
        mean_path_support_log = 0.0
        max_path_support_log = 0.0
        mean_pattern_weight = 0.0
        mean_neg_frac = 0.0
        max_neg_frac = 0.0
    return np.array([
        1.0,                                             # bias
        math.log(1 + row.path_length),                   # path_length_log
        1.0 if row.contains_virtual_edge else 0.0,       # contains_virtual_edge
        rule_support_frac,                                # rule_support_fraction
        mean_path_support_log,
        max_path_support_log,
        mean_pattern_weight,
        mean_neg_frac,
        max_neg_frac,
        math.log(1 + row.interval_corridor_count),       # interval_corridor_count_log
    ], dtype=np.float64)


def build_feature_matrix(
    rows: list[CorridorRow],
) -> tuple[list[CorridorFeatureVector], np.ndarray]:
    """Return (feature records, feature matrix). The matrix is rows ×
    len(FEATURE_NAMES) for direct regression."""
    vectors: list[CorridorFeatureVector] = []
    for row in rows:
        features = _featurize_one(row)
        vectors.append(CorridorFeatureVector(
            corridor_id=row.corridor_id,
            map_id=row.map_id,
            features=features,
            corridor_confidence=row.corridor_confidence,
        ))
    if not vectors:
        return [], np.zeros((0, len(FEATURE_NAMES)), dtype=np.float64)
    matrix = np.stack([v.features for v in vectors])
    return vectors, matrix
