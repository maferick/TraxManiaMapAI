"""Corridor-confidence scoring: combine the four evidence signals
into a single ``[0, 1]`` confidence per corridor path.

Design note §6.1 defines four per-edge signals:

- ``rule_support`` (bool) — seed-admitted under the Phase 2 family
  classification
- ``path_support_count`` (int) — how many enumerated corridor paths
  traverse this edge across the map (Signal 1)
- ``pattern_weight`` (float [0, 1]) — cross-map family-pair
  frequency, log-normalized (Signal 3)
- ``negative_evidence_count`` (int, max 12) — count of NON_DRIVABLE
  axis-neighbors across both endpoints (Signal 4; higher = more
  deco-clustered)

Plus one per-corridor signal from route_corridors:

- ``contains_virtual_edge`` (bool) — whether the path includes a
  replay-observation virtual edge

Scoring philosophy:

- **rule_support is a hard gate.** No-rule-support edge → corridor
  confidence 0. The classification already rejects edges that don't
  pass the seed rule; admitting them here would let the evaluator
  reward classification violations.
- **Per-edge score mixes path-support prior, pattern prior, and
  a deco-downweight.** Tuning is simple and published here; not
  learned yet.
- **Corridor = min(edge scores).** Weakest-link semantics. Averaging
  would let a corridor full of mediocre edges outscore a corridor
  with one clean long section plus one weak join. Weakest link
  matches the user's framing that a bad link kills a route.
- **Virtual-edge downweight at the corridor level.** Virtual edges
  are observation-derived connectivity assertions, not grid-edge
  assertions — less confident than a real grid traversal. Multiply
  by a fixed factor (0.8) when present. Not dependent on how many
  virtual edges — any virtual edge triggers the same discount.

Tuning constants live below with the rationale in comments. Consumers
that want to re-tune should bump ``SCORE_VERSION`` so downstream
caches know to re-score.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Bump when any weight in the scoring formula changes. Downstream
# caches (route_corridors.score_version) detect stale confidence
# and can trigger a re-score.
SCORE_VERSION: str = "0.1.0"

# Baseline contribution for a rule-supported edge with no other
# positive evidence. Not zero — a seed-valid edge is already evidence
# of drivability under the classification; starting at 0 would say
# "no evidence means no confidence," which would require a replay
# observation for every edge to have any score at all.
_BASELINE: float = 0.5

# Weight on path_support_count contribution. The path_support signal
# is the most direct "this edge is useful for racing" signal we have;
# it gets the biggest share of the non-baseline budget.
_PATH_SUPPORT_WEIGHT: float = 0.3

# Weight on pattern_weight contribution. Cross-map family-pair
# frequency is a weak prior — common transitions are more likely to
# be useful, but frequency is NOT validity per CLAUDE.md.
_PATTERN_WEIGHT: float = 0.2

# Per-edge downweight when deco-clustered. At full saturation
# (negative_evidence_count = 12), multiplies confidence by (1 - 0.5)
# = 0.5. A fully deco-surrounded edge loses half its confidence,
# doesn't zero out.
_DECO_DOWNWEIGHT: float = 0.5

# Multiplier applied once if the corridor contains any virtual edge.
# 0.8 keeps observation-derived corridors competitive but distinctly
# below pure grid corridors. Not dependent on virtual-edge count.
_VIRTUAL_EDGE_FACTOR: float = 0.8

# Max possible NON_DRIVABLE neighbors across both endpoints (6 axis
# neighbors × 2 cells). Used to normalize Signal 4 into [0, 1].
_MAX_NEG_EVIDENCE: int = 12


@dataclass(frozen=True)
class EdgeEvidence:
    """The per-edge fields needed to score an edge's confidence."""
    rule_support: bool
    path_support_count: int
    pattern_weight: float
    negative_evidence_count: int


def score_edge(
    ev: EdgeEvidence,
    *,
    per_map_max_path_support: int,
) -> float:
    """Score a single edge's confidence ∈ [0, 1].

    ``per_map_max_path_support`` normalizes path_support_count across
    edges on the same map — on a map where the hottest edge has
    support=50, an edge with support=10 shouldn't look weak.

    Returns 0.0 unconditionally when ``rule_support`` is false —
    hard gate. The scoring formula is:

        if not rule_support: return 0
        path_boost = log(1 + path_support_count) / log(1 + per_map_max)   # [0, 1]
        raw = baseline + path_weight × path_boost + pattern_weight × pw
        deco = 1 - deco_downweight × (neg_count / 12)
        score = clip(raw × deco, 0, 1)
    """
    if not ev.rule_support:
        return 0.0
    # path_boost: log-normalize so a single path supporter isn't
    # immediately dominated by a hot edge in the same map.
    if per_map_max_path_support > 0 and ev.path_support_count > 0:
        path_boost = math.log(1 + ev.path_support_count) / math.log(1 + per_map_max_path_support)
    else:
        path_boost = 0.0
    raw = (
        _BASELINE
        + _PATH_SUPPORT_WEIGHT * path_boost
        + _PATTERN_WEIGHT * max(0.0, min(1.0, ev.pattern_weight))
    )
    # Deco downweight: at full saturation, ev.negative_evidence_count =
    # 12 → multiply raw by (1 - _DECO_DOWNWEIGHT).
    neg_norm = max(0.0, min(1.0, ev.negative_evidence_count / _MAX_NEG_EVIDENCE))
    deco_factor = 1.0 - _DECO_DOWNWEIGHT * neg_norm
    return max(0.0, min(1.0, raw * deco_factor))


def score_corridor(
    edge_evidences: list[EdgeEvidence],
    *,
    contains_virtual_edge: bool,
    per_map_max_path_support: int,
) -> float:
    """Aggregate edge scores into one corridor confidence. Weakest-
    link: min across edges. Zero-edge corridors (self-paths of a
    single cell) return the baseline to indicate "no information"
    rather than 0 — a single-cell spawn=goal corridor isn't a
    racing path but isn't zero-evidence either.

    Virtual-edge downweight is applied once per corridor, not per
    virtual edge — any virtual edge hits the same discount.
    """
    if not edge_evidences:
        return _BASELINE
    per_edge = [
        score_edge(ev, per_map_max_path_support=per_map_max_path_support)
        for ev in edge_evidences
    ]
    confidence = min(per_edge)
    if contains_virtual_edge:
        confidence *= _VIRTUAL_EDGE_FACTOR
    return max(0.0, min(1.0, confidence))
