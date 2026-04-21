"""Zero-motion rule.

Detects contiguous windows where the player barely moves. A brief
pause (AFK at the start, bail near the finish) is a WARN. A long
idle, or many idles, promote to REJECT.
"""
from __future__ import annotations

from typing import Any, Mapping

from src.replay.rules.base import Rule, RuleResult
from src.replay.telemetry import ReplayTelemetry


class ZeroMotionRule(Rule):
    name = "zero_motion"
    version = "1.0.0"
    default_thresholds = {
        "warn_idle_ms": 3_000,
        "reject_idle_ms": 10_000,
        "min_motion_m_per_sample": 0.05,
    }

    def _evaluate(self, telemetry: ReplayTelemetry, thresholds: Mapping[str, Any]) -> RuleResult:
        warn_ms = int(thresholds["warn_idle_ms"])
        reject_ms = int(thresholds["reject_idle_ms"])
        min_motion = float(thresholds["min_motion_m_per_sample"])
        if warn_ms > reject_ms:
            raise ValueError("warn_idle_ms must be <= reject_idle_ms")

        samples = telemetry.samples
        worst_ms = 0
        worst_end_index = -1
        run_start_time = samples[0].time_ms
        run_start_index = 0

        for i in range(1, len(samples)):
            prev = samples[i - 1]
            curr = samples[i]
            dx = curr.x - prev.x
            dy = curr.y - prev.y
            dz = curr.z - prev.z
            delta = (dx * dx + dy * dy + dz * dz) ** 0.5
            if delta >= min_motion:
                run_start_time = curr.time_ms
                run_start_index = i
                continue
            idle_ms = curr.time_ms - run_start_time
            if idle_ms > worst_ms:
                worst_ms = idle_ms
                worst_end_index = i
                worst_start_index = run_start_index  # noqa: F841

        if worst_ms >= reject_ms:
            return self._reject(
                reason="long_idle_window",
                worst_idle_ms=worst_ms,
                reject_idle_ms=reject_ms,
                at_sample_index=worst_end_index,
            )
        if worst_ms >= warn_ms:
            return self._warn(
                reason="idle_window",
                worst_idle_ms=worst_ms,
                warn_idle_ms=warn_ms,
                at_sample_index=worst_end_index,
            )
        return self._pass(worst_idle_ms=worst_ms)
