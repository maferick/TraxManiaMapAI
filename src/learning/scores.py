"""Synthetic scores for the operator-facing dashboard.

**What these scores are**: deterministic, auditable combinations of
already-computed pipeline metrics — no hidden modeling, no fitted
weights. Each score is documented in terms of its inputs so a reader
can sanity-check it against the underlying numbers shown elsewhere
on the dashboard.

**What they are not**: not ground truth of "is the AI good".
They're operator-facing summaries that answer the question
"should I care?" at a glance. The diagnostic reports remain the
load-bearing evidence.

Three scores:

- :func:`ai_quality_score` — [0, 1] combining test rank correlation,
  prediction-stdev ratio vs heuristic, and AUC delta vs heuristic.
  Higher = the current learned model is beating the heuristic on
  multiple axes at once.
- :func:`variety_score` — [0, 1] derived from the A3 diversity
  watchdog's delta median. 1.0 means learned and heuristic pick
  equally diverse top-K; below 0.5 means measurable collapse.
- :func:`generation_readiness` — rule-based boolean + human-readable
  reasons. Opinionated: strict-enough that a "ready" answer is
  earned, loose-enough that it isn't unreachable for the current
  corpus.

All thresholds live at the top of this module so future review is
one-file.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


# ---------------------------------------------------------------------
# Thresholds — grouped so a reviewer can eyeball them in one place
# ---------------------------------------------------------------------

# AI Quality Score inputs:
# - test_rank_corr: Spearman rho on held-out; [0, 1] is "usable" range
#   (negative rank corr means the model is anti-aligned with labels,
#   which is never "good" by any definition).
# - pred_stdev_ratio: learned_stdev / heuristic_stdev. A ratio near 1
#   means the learned model has the same expressiveness as the hand-
#   tuned heuristic; <0.5 means it's visibly flatter.
# - auc_delta: learned AUC minus heuristic AUC on the proxy cohorts.
#   +0.15 is a meaningful win, +0.30 is strong.

_AIQ_RANK_CORR_FLOOR = 0.10      # below this → model is random-ish
_AIQ_RANK_CORR_CEIL = 0.50       # above this → rank corr axis is saturated
_AIQ_STDEV_RATIO_FLOOR = 0.30    # below this → model prediction is flat
_AIQ_STDEV_RATIO_CEIL = 1.00     # above this → model matches heuristic spread
_AIQ_AUC_DELTA_FLOOR = 0.0       # positive delta starts counting
_AIQ_AUC_DELTA_CEIL = 0.25       # above this → AUC axis is saturated

# Variety Score:
# - Inputs from the A3 watchdog: delta_median = learned_diversity -
#   heuristic_diversity. 0 means perfect parity; negative means
#   learned collapses more than heuristic.
# - 1.0 at delta >= 0 (no collapse vs heuristic).
# - 0.0 at delta <= -0.20 (severe collapse).
# - Linear between.

_VARIETY_DELTA_PERFECT = 0.0
_VARIETY_DELTA_ZERO = -0.20

# Generation readiness gates (Phase 2 v0):
# - Need the learned model to be non-trivially good.
# - Need diversity not collapsing.
# - Need enough data coverage that labels are meaningful.

_READY_MIN_AI_QUALITY = 0.40
_READY_MIN_VARIETY = 0.70
_READY_MIN_LABEL_COVERAGE = 0.10  # frac of corridor-owning maps with a label
_READY_MIN_LEARNED_COVERAGE = 0.80  # frac of top-rank corridors scored


@dataclass(frozen=True)
class QualityInputs:
    """Inputs to :func:`ai_quality_score`. Allow ``None`` for the
    per-axis values that don't always exist — scoring just skips the
    missing axis and re-normalises the weights."""
    test_rank_corr: float | None = None
    pred_stdev_ratio: float | None = None
    auc_delta: float | None = None


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _axis_score(v: float, floor: float, ceil: float) -> float:
    """Linearly map [floor, ceil] → [0, 1]; saturate on either side."""
    if ceil <= floor:
        return 0.0
    return _clamp((v - floor) / (ceil - floor), 0.0, 1.0)


def ai_quality_score(inputs: QualityInputs) -> float | None:
    """Combine the three quality axes into a single [0, 1] score.

    Equal weight across populated axes. Returns ``None`` when no axes
    are available — don't report a synthetic "0" on missing data."""
    axes: list[float] = []
    if inputs.test_rank_corr is not None:
        axes.append(_axis_score(
            inputs.test_rank_corr,
            _AIQ_RANK_CORR_FLOOR, _AIQ_RANK_CORR_CEIL,
        ))
    if inputs.pred_stdev_ratio is not None:
        axes.append(_axis_score(
            inputs.pred_stdev_ratio,
            _AIQ_STDEV_RATIO_FLOOR, _AIQ_STDEV_RATIO_CEIL,
        ))
    if inputs.auc_delta is not None:
        axes.append(_axis_score(
            inputs.auc_delta,
            _AIQ_AUC_DELTA_FLOOR, _AIQ_AUC_DELTA_CEIL,
        ))
    if not axes:
        return None
    return sum(axes) / len(axes)


def variety_score(delta_median: float | None) -> float | None:
    """Map the A3 diversity watchdog's delta_median into [0, 1].

    ``None`` when the watchdog couldn't produce a number (e.g. no
    learned scores yet)."""
    if delta_median is None:
        return None
    if delta_median >= _VARIETY_DELTA_PERFECT:
        return 1.0
    if delta_median <= _VARIETY_DELTA_ZERO:
        return 0.0
    return (delta_median - _VARIETY_DELTA_ZERO) / (
        _VARIETY_DELTA_PERFECT - _VARIETY_DELTA_ZERO
    )


@dataclass(frozen=True)
class ReadinessReport:
    """Generation-readiness check result. ``ready`` is the strict
    boolean; ``reasons`` enumerates what failed (or confirmed) so the
    dashboard can explain the verdict."""
    ready: bool
    reasons: list[str]
    # Normalised [0, 1] fractional readiness — for trend display.
    # 1.0 = all gates passed; lower = fraction of gates passing.
    fraction: float


def generation_readiness(
    *,
    ai_quality: float | None,
    variety: float | None,
    label_coverage: float | None,
    learned_coverage: float | None,
) -> ReadinessReport:
    """Strict-ish readiness rule.

    Gates (all must pass):
    - ai_quality >= 0.40
    - variety >= 0.70
    - label_coverage >= 0.10
    - learned_coverage >= 0.80

    ``reasons`` lists each gate with its pass/fail verdict so the UI
    never shows "not ready" without explaining why."""
    gates = [
        ("AI Quality",
         ai_quality, _READY_MIN_AI_QUALITY),
        ("Variety",
         variety, _READY_MIN_VARIETY),
        ("Label coverage",
         label_coverage, _READY_MIN_LABEL_COVERAGE),
        ("Learned coverage",
         learned_coverage, _READY_MIN_LEARNED_COVERAGE),
    ]
    reasons: list[str] = []
    passed = 0
    missing_inputs = 0
    for label, val, floor in gates:
        if val is None:
            reasons.append(f"{label}: data unavailable")
            missing_inputs += 1
            continue
        if val >= floor:
            reasons.append(f"{label}: OK ({val:.2f} ≥ {floor:.2f})")
            passed += 1
        else:
            reasons.append(f"{label}: below floor ({val:.2f} < {floor:.2f})")
    total = len(gates)
    considered = total - missing_inputs
    fraction = passed / total  # missing inputs count as failing
    ready = (passed == total)
    return ReadinessReport(ready=ready, reasons=reasons, fraction=fraction)


@dataclass(frozen=True)
class TrendSample:
    """One historical score sample. ``value`` ``None`` means the metric
    wasn't available for that sample — skip it in trend calc."""
    recorded_at_unix: float
    value: float | None


def trend_direction(samples: Iterable[TrendSample]) -> str:
    """Classify a score's trend across the provided samples (oldest →
    newest). Returns one of: ``improving`` | ``flat`` | ``worsening``
    | ``unknown``.

    Uses a simple comparison of the first-third mean vs last-third
    mean — no regressions, no fitting — so the classification is
    reproducible and easily defensible."""
    values = [s.value for s in samples if s.value is not None]
    if len(values) < 2:
        return "unknown"
    third = max(1, len(values) // 3)
    head = sum(values[:third]) / third
    tail = sum(values[-third:]) / third
    delta = tail - head
    # Threshold: movement below 0.02 on a [0,1] score is noise.
    if abs(delta) < 0.02:
        return "flat"
    return "improving" if delta > 0 else "worsening"
