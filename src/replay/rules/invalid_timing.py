"""Invalid-timing rule.

REJECT when sample timestamps go backwards.
REJECT when any inter-sample gap exceeds ``max_gap_factor`` × the
nominal period (1000 / ``sample_rate_hz``) AND that gap is wider
than ``hard_gap_ms``. The two-conditional guard avoids false
positives on wrappers that declare a higher rate than they actually
sample.
WARN on any gap above the factor (but below the hard cap).
"""
from __future__ import annotations

from typing import Any, Mapping

from src.replay.rules.base import Rule, RuleResult
from src.replay.telemetry import ReplayTelemetry


class InvalidTimingRule(Rule):
    name = "invalid_timing"
    version = "1.0.0"
    default_thresholds = {
        "max_gap_factor": 3.0,
        "hard_gap_ms": 2_000,
    }

    def _evaluate(self, telemetry: ReplayTelemetry, thresholds: Mapping[str, Any]) -> RuleResult:
        max_factor = float(thresholds["max_gap_factor"])
        hard_gap_ms = int(thresholds["hard_gap_ms"])
        nominal_ms = 1000.0 / telemetry.sample_rate_hz
        soft_limit_ms = max_factor * nominal_ms

        samples = telemetry.samples
        worst_gap_ms = 0
        worst_gap_index = -1
        backwards_index = -1

        for i in range(1, len(samples)):
            dt = samples[i].time_ms - samples[i - 1].time_ms
            if dt < 0 and backwards_index < 0:
                backwards_index = i
            if dt > worst_gap_ms:
                worst_gap_ms = dt
                worst_gap_index = i

        if backwards_index >= 0:
            return self._reject(
                reason="non_monotonic_timestamps",
                at_sample_index=backwards_index,
                delta_ms=samples[backwards_index].time_ms - samples[backwards_index - 1].time_ms,
            )

        if worst_gap_ms > hard_gap_ms:
            return self._reject(
                reason="excessive_gap",
                worst_gap_ms=worst_gap_ms,
                at_sample_index=worst_gap_index,
                hard_gap_ms=hard_gap_ms,
            )

        if worst_gap_ms > soft_limit_ms:
            return self._warn(
                reason="gap_above_nominal",
                worst_gap_ms=worst_gap_ms,
                nominal_ms=nominal_ms,
                max_gap_factor=max_factor,
                at_sample_index=worst_gap_index,
            )

        return self._pass(
            worst_gap_ms=worst_gap_ms,
            nominal_ms=nominal_ms,
        )
