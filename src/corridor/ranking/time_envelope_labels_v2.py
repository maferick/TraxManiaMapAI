"""Time-envelope label v2 — variance-aware + robust aggregation.

Sits alongside ``time_envelope_labels.py`` (v1). Same label dimension
(plausibility in ``(0, 1]`` per corridor), with three additions:

- **Pluggable aggregation** — mean (default, v1 behavior), median,
  or trimmed-mean. Robust to outlier runs.
- **Outlier rejection before aggregation** — drop replay gaps
  beyond ``k*sigma`` from the initial-pass mean (default ``k=3.0``;
  ``None`` disables).
- **Label quality weight** — a separate ``(0, 1]`` value per corridor
  computed from the coefficient of variation across observed inter-CP
  times. High-variance maps (drivers disagree) get lower label
  quality. Consumers use this as a sample-weight in training.

Design principles (audited in the module header):

- **No leakage.** Inputs: observed replay checkpoint_times_ms, path
  length, global speed prior, block size. Nothing rank-derived,
  cohort-derived, or learned-score-derived.
- **Honesty over convenience.** The plausibility *value* is still
  v1-compatible; quality lives in a separate field so downstream
  can decide whether to weight by it. No conflating "length fits"
  with "label trustworthy."
- **Provenance.** Every run emits a metadata dict (``LabelMetadata``)
  capturing aggregator, trim quantile, outlier sigma, speed prior,
  per-map replay count, and a scheme version.

See ``docs/learning/time-envelope-label-v2.md`` for the full spec +
what's deliberately not in scope (per-segment labels, cohort-aware
aggregation, per-family speed priors).
"""
from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pymysql.connections import Connection

# Reuse the v1 pure plausibility function and module constants so v1
# and v2 stay numerically comparable on the value axis.
from src.corridor.ranking.features import CorridorRow
from src.corridor.ranking.time_envelope_labels import (
    _BLOCK_SIZE_M,
    _DEFAULT_SPEED_PRIOR_M_S,
    plausibility,
)
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


_SCHEME_VERSION: str = "time_envelope_v2@0.2.0"

AggregationMethod = Literal["mean", "median", "trimmed_mean"]


@dataclass(frozen=True)
class MapIntervalStats:
    """Per-map aggregation result. All fields derived purely from
    observed replay timing + the chosen aggregator."""
    map_id: int
    aggregated_interval_ms: float      # the "map mean" the v1 label used
    interval_stdev_ms: float           # across the accepted gap sample
    replay_count_used: int             # after outlier rejection
    coefficient_of_variation: float    # stdev / mean
    label_quality_weight: float        # 1 / (1 + cv)


@dataclass
class LabelMetadata:
    """Provenance. Persisted alongside the model JSON."""
    label_scheme: str = _SCHEME_VERSION
    scheme_version: str = _SCHEME_VERSION
    aggregation_method: AggregationMethod = "trimmed_mean"
    trimmed_q: float = 0.1
    outlier_rejection_sigma: float | None = 3.0
    speed_prior_m_s: float = _DEFAULT_SPEED_PRIOR_M_S
    block_size_m: float = _BLOCK_SIZE_M
    replay_count_per_map: dict[int, int] = field(default_factory=dict)
    generated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "label_scheme": self.label_scheme,
            "scheme_version": self.scheme_version,
            "aggregation_method": self.aggregation_method,
            "trimmed_q": self.trimmed_q,
            "outlier_rejection_sigma": self.outlier_rejection_sigma,
            "speed_prior_m_s": self.speed_prior_m_s,
            "block_size_m": self.block_size_m,
            "replay_count_per_map": dict(self.replay_count_per_map),
            "generated_at": self.generated_at,
        }


# ---------------------------------------------------------------------
# Aggregation primitives — pure, testable without a DB
# ---------------------------------------------------------------------

def _aggregate(values: list[float], method: AggregationMethod, trimmed_q: float) -> float:
    """Apply the selected aggregator. ``values`` must be non-empty."""
    if not values:
        raise ValueError("_aggregate requires non-empty values")
    if method == "mean":
        return statistics.mean(values)
    if method == "median":
        return statistics.median(values)
    if method == "trimmed_mean":
        if trimmed_q < 0 or trimmed_q >= 0.5:
            raise ValueError("trimmed_q must be in [0, 0.5)")
        if trimmed_q == 0:
            return statistics.mean(values)
        s = sorted(values)
        n = len(s)
        k = int(n * trimmed_q)
        trimmed = s[k: n - k] if n - 2 * k > 0 else s
        return statistics.mean(trimmed)
    raise ValueError(f"unknown aggregation method: {method!r}")


def _reject_outliers(
    values: list[float], sigma: float | None,
) -> list[float]:
    """Drop values more than ``sigma`` stdevs from the sample mean.
    ``sigma=None`` disables (returns the list unchanged). Needs ≥ 2
    values to compute stdev — shorter lists pass through."""
    if sigma is None:
        return list(values)
    if len(values) < 2:
        return list(values)
    mean = statistics.mean(values)
    stdev = statistics.stdev(values)
    if stdev == 0:
        return list(values)
    return [v for v in values if abs(v - mean) <= sigma * stdev]


def compute_interval_stats(
    map_id: int,
    gaps: list[float],
    *,
    method: AggregationMethod = "trimmed_mean",
    trimmed_q: float = 0.1,
    outlier_sigma: float | None = 3.0,
) -> MapIntervalStats | None:
    """Pure function: raw gap list → (aggregated_mean, stdev, cv, weight).
    Returns ``None`` if the sample is empty after outlier rejection."""
    accepted = _reject_outliers(gaps, outlier_sigma)
    if not accepted:
        return None
    agg = _aggregate(accepted, method, trimmed_q)
    stdev = statistics.stdev(accepted) if len(accepted) >= 2 else 0.0
    cv = (stdev / agg) if agg > 0 else 0.0
    quality = 1.0 / (1.0 + cv) if cv >= 0 else 1.0
    return MapIntervalStats(
        map_id=map_id,
        aggregated_interval_ms=float(agg),
        interval_stdev_ms=float(stdev),
        replay_count_used=len(accepted),
        coefficient_of_variation=float(cv),
        label_quality_weight=float(quality),
    )


# ---------------------------------------------------------------------
# DB collection (mirrors v1 — same breadcrumb source)
# ---------------------------------------------------------------------

def _extract_gaps(times: list[float]) -> list[float]:
    """Gap list from a ``checkpoint_times_ms`` array. First gap is
    Spawn→CP1 (t=0 → times[0]). Drops non-monotonic entries."""
    if len(times) < 2:
        return []
    out: list[float] = [float(times[0])]
    for i in range(1, len(times)):
        gap = float(times[i]) - float(times[i - 1])
        if gap > 0:
            out.append(gap)
    return out


def load_map_interval_stats(
    conn: Connection,
    *,
    method: AggregationMethod = "trimmed_mean",
    trimmed_q: float = 0.1,
    outlier_sigma: float | None = 3.0,
    snapshot_id: str | None = None,
) -> dict[int, MapIntervalStats]:
    """Per-map :class:`MapIntervalStats`, aggregated across all clean
    replays. Maps with no qualifying replays are absent.

    ``snapshot_id`` (optional) restricts to replays whose parent map
    lives in the given ingestion snapshot — used for A/B comparison
    between snapshot cohorts.
    """
    with cursor(conn) as cur:
        if snapshot_id is None:
            cur.execute(
                """
                SELECT r.map_id, r.breadcrumbs_path
                FROM replays r
                WHERE r.clean_status IN ('clean','usable_with_warnings')
                  AND r.breadcrumbs_path IS NOT NULL
                  AND EXISTS (SELECT 1 FROM route_corridors rc WHERE rc.map_id = r.map_id)
                """
            )
        else:
            cur.execute(
                """
                SELECT r.map_id, r.breadcrumbs_path
                FROM replays r
                JOIN maps m ON m.id = r.map_id
                WHERE r.clean_status IN ('clean','usable_with_warnings')
                  AND r.breadcrumbs_path IS NOT NULL
                  AND m.ingestion_snapshot = %s
                  AND EXISTS (SELECT 1 FROM route_corridors rc WHERE rc.map_id = r.map_id)
                """,
                (snapshot_id,),
            )
        rows = cur.fetchall()

    by_map: dict[int, list[float]] = {}
    for map_id, bc_path in rows:
        try:
            payload = json.loads(Path(bc_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        times = payload.get("checkpoint_times_ms")
        if not isinstance(times, list):
            continue
        gaps = _extract_gaps(times)
        if not gaps:
            continue
        by_map.setdefault(int(map_id), []).extend(gaps)

    out: dict[int, MapIntervalStats] = {}
    for mid, gaps in by_map.items():
        stats = compute_interval_stats(
            mid, gaps,
            method=method, trimmed_q=trimmed_q, outlier_sigma=outlier_sigma,
        )
        if stats is not None:
            out[mid] = stats
    return out


# ---------------------------------------------------------------------
# Label synthesis
# ---------------------------------------------------------------------

def synthesize_time_envelope_v2_labels(
    rows: list[CorridorRow],
    map_stats: dict[int, MapIntervalStats],
    *,
    speed_prior_m_s: float = _DEFAULT_SPEED_PRIOR_M_S,
) -> tuple[dict[int, float], dict[int, float]]:
    """Return ``(labels, label_quality_weights)``.

    - ``labels[corridor_id]`` = plausibility (same value axis as v1)
    - ``label_quality_weights[corridor_id]`` = per-corridor weight in
      ``(0, 1]`` derived from the map's observed-time CV.

    Corridors on maps without aggregated stats are silently omitted
    — the caller drops them from the training set.
    """
    labels: dict[int, float] = {}
    quality: dict[int, float] = {}
    for row in rows:
        stats = map_stats.get(row.map_id)
        if stats is None:
            continue
        labels[row.corridor_id] = plausibility(
            path_length_cells=row.path_length,
            observed_elapsed_ms=stats.aggregated_interval_ms,
            speed_prior_m_s=speed_prior_m_s,
        )
        quality[row.corridor_id] = stats.label_quality_weight
    return labels, quality


def build_metadata(
    map_stats: dict[int, MapIntervalStats],
    *,
    method: AggregationMethod,
    trimmed_q: float,
    outlier_sigma: float | None,
    speed_prior_m_s: float,
) -> LabelMetadata:
    return LabelMetadata(
        aggregation_method=method,
        trimmed_q=trimmed_q,
        outlier_rejection_sigma=outlier_sigma,
        speed_prior_m_s=speed_prior_m_s,
        block_size_m=_BLOCK_SIZE_M,
        replay_count_per_map={
            mid: stats.replay_count_used for mid, stats in map_stats.items()
        },
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
    )
