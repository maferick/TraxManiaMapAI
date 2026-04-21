"""Spectator-artifact rule.

A very cheap proxy for "someone actually raced": total path length.
Spectator-camera replays, pause-screen captures, and mid-finish
disconnects all accumulate very little path length regardless of
duration. A real racing run on even the shortest TM2020 track
traverses tens of meters at minimum.
"""
from __future__ import annotations

from typing import Any, Mapping

from src.replay.rules.base import Rule, RuleResult
from src.replay.telemetry import ReplayTelemetry


class SpectatorRule(Rule):
    name = "spectator"
    version = "1.0.0"
    default_thresholds = {
        "min_total_distance_m": 20.0,
    }

    def _evaluate(self, telemetry: ReplayTelemetry, thresholds: Mapping[str, Any]) -> RuleResult:
        min_distance = float(thresholds["min_total_distance_m"])
        samples = telemetry.samples

        total_m = 0.0
        for i in range(1, len(samples)):
            prev = samples[i - 1]
            curr = samples[i]
            dx = curr.x - prev.x
            dy = curr.y - prev.y
            dz = curr.z - prev.z
            total_m += (dx * dx + dy * dy + dz * dz) ** 0.5

        if total_m < min_distance:
            return self._reject(
                reason="insufficient_path_length",
                total_distance_m=total_m,
                min_total_distance_m=min_distance,
            )
        return self._pass(total_distance_m=total_m)
