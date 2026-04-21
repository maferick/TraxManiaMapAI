"""Incomplete-replay rule.

REJECT when the replay is unmistakably truncated:
- too few samples, OR
- duration below the floor AND no finish event

WARN when the replay ran long enough but never reached the finish
(spin-out, bail, mid-run disconnect).
"""
from __future__ import annotations

from typing import Any, Mapping

from src.replay.rules.base import Rule, RuleResult
from src.replay.telemetry import ReplayTelemetry


class IncompleteRule(Rule):
    name = "incomplete"
    version = "1.0.0"
    default_thresholds = {
        "min_samples": 50,
        "min_duration_ms": 5_000,
    }

    def _evaluate(self, telemetry: ReplayTelemetry, thresholds: Mapping[str, Any]) -> RuleResult:
        min_samples = int(thresholds["min_samples"])
        min_duration_ms = int(thresholds["min_duration_ms"])

        sample_count = len(telemetry.samples)
        if sample_count < min_samples:
            return self._reject(
                reason="too_few_samples",
                sample_count=sample_count,
                min_samples=min_samples,
            )

        duration_ms = telemetry.duration_ms
        if not telemetry.finished and duration_ms < min_duration_ms:
            return self._reject(
                reason="too_short_no_finish",
                duration_ms=duration_ms,
                min_duration_ms=min_duration_ms,
            )

        if not telemetry.finished:
            return self._warn(
                reason="no_finish_event",
                duration_ms=duration_ms,
            )

        return self._pass(
            sample_count=sample_count,
            duration_ms=duration_ms,
            finish_time_ms=telemetry.finish_time_ms,
        )
