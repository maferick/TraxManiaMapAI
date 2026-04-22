"""Synthetic labels for corridor ranking.

Ground truth (which corridor is the "real" racing line) is
unavailable — we don't have position telemetry. Synthetic labels
have to be honest about that: they're proxies, not truth. The
model is learning "things a driver's path probably looks like,"
not "this corridor was driven."

v0.1 label: ``inverse rank within interval``.

For each interval, corridors are sorted by the same
shortest-first-lex-tiebreak rule the enumeration uses (``path_rank``
in route_corridors). Label = ``1 - rank/(N-1)`` → the top rank gets
label 1.0, the last rank gets 0.0, evenly spaced between. Intervals
with only one corridor get label 0.5 (neutral — no ranking info).

Why this label:

- Directly available without external data
- The model learns what features distinguish rank-0 corridors from
  the rest (which may include features the heuristic doesn't use —
  interval_corridor_count, evidence-distribution statistics, etc.)
- The COMPARISON against corridor_confidence answers: "does a
  learned re-weighting of features predict rank-0-ness better than
  the hand-tuned score?"

What this label CAN'T do:

- It can't tell us whether rank-0 corridors are actually correct —
  "shortest + lex" is a convention, not truth
- It can't discriminate between two equally-good corridors at
  different ranks — the model will arbitrarily prefer whichever
  tiebreak chose rank-0
- It rewards simplicity (shorter corridors) more than it should

A follow-up label scheme — time-envelope plausibility against
replay checkpoint elapsed_ms — would ground the labels in
observed-driver behavior. Deferred to a v0.2 label pass.
"""
from __future__ import annotations

from collections import defaultdict

from src.corridor.ranking.features import CorridorRow


def synthesize_inverse_rank_labels(rows: list[CorridorRow]) -> dict[int, float]:
    """Return ``{corridor_id: label ∈ [0, 1]}`` where the top rank in
    each interval gets 1.0 and the last gets 0.0. Single-corridor
    intervals get 0.5 (no information).

    Intervals are keyed by (map_id, src_tag, src_order, dst_tag,
    dst_order). Within each interval, we use path_rank directly
    because build-route-corridors already materialized the canonical
    ordering.
    """
    by_interval: dict[tuple[int, str, int, str, int], list[CorridorRow]] = defaultdict(list)
    for r in rows:
        key = (r.map_id, r.src_tag, r.src_order, r.dst_tag, r.dst_order)
        by_interval[key].append(r)

    labels: dict[int, float] = {}
    for interval_rows in by_interval.values():
        n = len(interval_rows)
        if n == 1:
            labels[interval_rows[0].corridor_id] = 0.5
            continue
        # Sort by path_rank (canonical shortest+lex already applied).
        interval_rows.sort(key=lambda r: r.path_rank)
        for idx, row in enumerate(interval_rows):
            labels[row.corridor_id] = 1.0 - idx / (n - 1)
    return labels
