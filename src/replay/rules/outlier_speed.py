"""Outlier-speed rule.

Uses the velocity magnitude recorded in each sample (not derived from
positions). A small number of spikes can happen on special tracks
(gravity pads, boosters); ``max_spike_count`` is the WARN→REJECT
cutoff. A single sample exceeding ``hard_cap_mps`` is always REJECT.
"""
from __future__ import annotations

from typing import Any, Mapping

from src.replay.rules.base import Rule, RuleResult
from src.replay.telemetry import ReplayTelemetry


class OutlierSpeedRule(Rule):
    name = "outlier_speed"
    version = "1.0.0"
    default_thresholds = {
        "max_speed_mps": 150.0,
        "hard_cap_mps": 250.0,
        "max_spike_count": 5,
    }

    def _evaluate(self, telemetry: ReplayTelemetry, thresholds: Mapping[str, Any]) -> RuleResult:
        warn_at = float(thresholds["max_speed_mps"])
        reject_at = float(thresholds["hard_cap_mps"])
        max_spikes = int(thresholds["max_spike_count"])
        if warn_at > reject_at:
            raise ValueError("max_speed_mps must be <= hard_cap_mps")

        samples = telemetry.samples
        top_speed = 0.0
        top_index = -1
        spike_count = 0
        reject_index = -1

        for i, s in enumerate(samples):
            speed = s.speed_mps
            if speed > top_speed:
                top_speed = speed
                top_index = i
            if speed >= reject_at and reject_index < 0:
                reject_index = i
            if speed > warn_at:
                spike_count += 1

        if reject_index >= 0:
            return self._reject(
                reason="speed_hard_cap_exceeded",
                at_sample_index=reject_index,
                top_speed_mps=top_speed,
                hard_cap_mps=reject_at,
            )
        if spike_count > max_spikes:
            return self._reject(
                reason="too_many_speed_spikes",
                spike_count=spike_count,
                max_spike_count=max_spikes,
                top_speed_mps=top_speed,
            )
        if spike_count > 0:
            return self._warn(
                reason="speed_spikes_above_soft",
                spike_count=spike_count,
                top_speed_mps=top_speed,
                max_speed_mps=warn_at,
                at_sample_index=top_index,
            )
        return self._pass(top_speed_mps=top_speed)
