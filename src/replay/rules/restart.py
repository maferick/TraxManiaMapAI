"""Restart-pattern rule.

Uses the wrapper-reported ``restart_sample_indices`` as the primary
signal. A replay with explicit restart events has been through one or
more in-run resets; 0 passes, a few warn, many reject.
"""
from __future__ import annotations

from typing import Any, Mapping

from src.replay.rules.base import Rule, RuleResult
from src.replay.telemetry import ReplayTelemetry


class RestartRule(Rule):
    name = "restart"
    version = "1.0.0"
    default_thresholds = {
        "warn_at_count": 1,
        "reject_at_count": 3,
    }

    def _evaluate(self, telemetry: ReplayTelemetry, thresholds: Mapping[str, Any]) -> RuleResult:
        warn_at = int(thresholds["warn_at_count"])
        reject_at = int(thresholds["reject_at_count"])
        if warn_at > reject_at:
            raise ValueError("warn_at_count must be <= reject_at_count")

        count = len(telemetry.restart_sample_indices)
        if count >= reject_at:
            return self._reject(
                reason="too_many_restarts",
                restart_count=count,
                reject_at_count=reject_at,
            )
        if count >= warn_at:
            return self._warn(
                reason="restart_events_present",
                restart_count=count,
                warn_at_count=warn_at,
            )
        return self._pass(restart_count=count)
