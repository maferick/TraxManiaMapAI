"""Time-envelope labels for corridor ranking.

Honest name: these are *weak observed labels*, not ground truth.
The label scheme answers "does this corridor's length fit the time
an actual driver took to complete the interval?" Short corridors
that can't physically be the driven path (because the elapsed time
implies much more distance) score low; length-plausible corridors
score high.

Per-map aggregation: for each map, average the inter-checkpoint
elapsed times across all clean replays on that map. That becomes
the map's "typical interval time." Per-corridor expected time is
``path_length_cells × block_size / speed_prior``. Plausibility is
a smooth decay on the relative time error.

Limitations (recorded here so future readers don't over-trust the
signal):

- Speed prior is a global constant. TM2020 speeds vary 10–80 m/s
  by surface + map style. A per-family prior is a natural v0.2.
- For plain-Checkpoint maps, the route_corridors enumeration only
  stores ``Spawn → Checkpoint`` (one merged anchor set) and
  ``Spawn → Goal``. We can't align individual CP times to individual
  CP block cells — so the per-interval time used is the MAP-mean
  of inter-checkpoint gaps, not the specific checkpoint this
  corridor targets. This blurs the label but avoids inventing
  ordering we don't have.
- Corridors whose map has no clean replays produce no time-envelope
  label. Training drops them — deliberately; no observation, no
  label. Keep the synthetic inverse-rank scheme alongside so those
  corridors still contribute there.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from pathlib import Path

from pymysql.connections import Connection

from src.corridor.ranking.features import CorridorRow
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


# TM2020 block grid cells are ~32m × 8m × 32m; path length is counted
# in cells, which under-estimates distance on jumps (cell-to-cell
# diagonal through air counts as 1) but is the best-effort available.
_BLOCK_SIZE_M: float = 32.0

# Default global speed prior. Calibrated by inspecting replay mean
# interval times on the scale-1k corpus against the median corridor
# length they produce (both land at roughly 30 m/s effective).
# This is a proxy, not a physical constant.
_DEFAULT_SPEED_PRIOR_M_S: float = 30.0


def plausibility(
    path_length_cells: int,
    observed_elapsed_ms: float,
    *,
    speed_prior_m_s: float = _DEFAULT_SPEED_PRIOR_M_S,
    block_size_m: float = _BLOCK_SIZE_M,
) -> float:
    """Plausibility that a corridor of ``path_length_cells`` cells was
    the driven path given ``observed_elapsed_ms`` between checkpoints.

    ``expected_time_ms = path_length_cells × block_size / speed × 1000``
    ``rel_err = |observed - expected| / observed``
    ``plausibility = exp(-rel_err)``

    Exponential decay on relative error lands in ``(0, 1]``:

    - exact match → 1.0
    - expected twice observed → exp(-1) ≈ 0.37
    - expected 5× observed → exp(-4) ≈ 0.018

    Returns 0.0 for non-positive inputs (observed <= 0, path_length
    <= 0, speed_prior <= 0) since the formula is undefined.
    """
    if (path_length_cells <= 0 or observed_elapsed_ms <= 0
            or speed_prior_m_s <= 0 or block_size_m <= 0):
        return 0.0
    expected_time_ms = path_length_cells * block_size_m / speed_prior_m_s * 1000.0
    rel_err = abs(observed_elapsed_ms - expected_time_ms) / observed_elapsed_ms
    return math.exp(-rel_err)


def _load_map_mean_interval_ms(conn: Connection) -> dict[int, float]:
    """Per-map mean inter-checkpoint elapsed time, aggregated across
    all clean replays on that map. Map ids with no qualifying replays
    are absent from the result — callers must handle missing keys.
    """
    # Pull breadcrumb paths for all clean replays across all maps that
    # have corridors. One shot, then process sidecar files in Python.
    with cursor(conn) as cur:
        cur.execute(
            """
            SELECT r.map_id, r.breadcrumbs_path
            FROM replays r
            WHERE r.clean_status IN ('clean','usable_with_warnings')
              AND r.breadcrumbs_path IS NOT NULL
              AND EXISTS (SELECT 1 FROM route_corridors rc WHERE rc.map_id = r.map_id)
            """
        )
        rows = cur.fetchall()

    by_map: dict[int, list[float]] = {}
    for map_id, bc_path in rows:
        try:
            payload = json.loads(Path(bc_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        times = payload.get("checkpoint_times_ms")
        if not isinstance(times, list) or len(times) < 2:
            continue
        # Gaps between consecutive checkpoints + the initial gap from
        # time 0 to first checkpoint (since Spawn is at t=0).
        gaps: list[float] = [float(times[0])]
        for i in range(1, len(times)):
            gap = float(times[i]) - float(times[i - 1])
            if gap <= 0:
                continue  # non-monotonic (shouldn't happen on clean replays, but defend)
            gaps.append(gap)
        if not gaps:
            continue
        by_map.setdefault(int(map_id), []).extend(gaps)

    return {
        mid: statistics.mean(gaps)
        for mid, gaps in by_map.items()
        if gaps
    }


def synthesize_time_envelope_labels(
    rows: list[CorridorRow],
    map_mean_interval_ms: dict[int, float],
    *,
    speed_prior_m_s: float = _DEFAULT_SPEED_PRIOR_M_S,
) -> dict[int, float]:
    """Return ``{corridor_id: plausibility}`` for every corridor whose
    map has a mean interval time in ``map_mean_interval_ms``. Corridors
    on maps without replay data are silently omitted — the caller
    treats them as unlabeled and drops them from the time-envelope
    training set.
    """
    out: dict[int, float] = {}
    for row in rows:
        observed = map_mean_interval_ms.get(row.map_id)
        if observed is None:
            continue
        out[row.corridor_id] = plausibility(
            path_length_cells=row.path_length,
            observed_elapsed_ms=observed,
            speed_prior_m_s=speed_prior_m_s,
        )
    return out
