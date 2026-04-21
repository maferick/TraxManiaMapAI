"""Teleport rule.

A teleport is a per-sample position delta that violates physics. At
50 Hz and a generous 150 m/s (540 km/h) speed ceiling, the max
plausible per-tick delta is ~3 m. Any single jump of
``max_delta_m`` or more is treated as a telemetry artifact (or a
replay that spans a reset) — REJECT by default.
"""
from __future__ import annotations

from typing import Any, Mapping

from src.replay.rules.base import Rule, RuleResult
from src.replay.telemetry import ReplayTelemetry


class TeleportRule(Rule):
    name = "teleport"
    version = "1.0.0"
    default_thresholds = {
        "max_delta_m": 10.0,
        "soft_delta_m": 5.0,
    }

    def _evaluate(self, telemetry: ReplayTelemetry, thresholds: Mapping[str, Any]) -> RuleResult:
        reject_at = float(thresholds["max_delta_m"])
        warn_at = float(thresholds["soft_delta_m"])
        if warn_at > reject_at:
            raise ValueError("soft_delta_m must be <= max_delta_m")

        samples = telemetry.samples
        worst_delta_m = 0.0
        worst_index = -1
        reject_index = -1
        warn_count = 0

        for i in range(1, len(samples)):
            prev = samples[i - 1]
            curr = samples[i]
            dx = curr.x - prev.x
            dy = curr.y - prev.y
            dz = curr.z - prev.z
            delta = (dx * dx + dy * dy + dz * dz) ** 0.5
            if delta > worst_delta_m:
                worst_delta_m = delta
                worst_index = i
            if delta >= reject_at and reject_index < 0:
                reject_index = i
            elif delta >= warn_at:
                warn_count += 1

        if reject_index >= 0:
            return self._reject(
                reason="teleport_detected",
                at_sample_index=reject_index,
                delta_m=worst_delta_m,
                max_delta_m=reject_at,
            )
        if warn_count > 0:
            return self._warn(
                reason="large_position_jumps",
                worst_delta_m=worst_delta_m,
                at_sample_index=worst_index,
                jumps_above_soft=warn_count,
                soft_delta_m=warn_at,
            )
        return self._pass(worst_delta_m=worst_delta_m)
