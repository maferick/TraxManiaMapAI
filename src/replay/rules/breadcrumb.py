"""Cleaning rules that run on :class:`ReplayBreadcrumbs` instead of
:class:`ReplayTelemetry`.

Used when GBX.NET can't decode a replay's position samples
(the offline TM2020 blocker). Rules in this module restrict themselves
to signals present in the breadcrumbs sidecar:

- ``checkpoint_times_ms`` — exact race-phase anchors
- ``inputs[]`` — decoded IInput timeline (Accelerate / Brake /
  SteerTM2020 / MouseAccu / Respawn / ...)
- ``finish_time_ms``

Rules that need continuous position samples (teleport, outlier_speed,
zero_motion) do NOT have breadcrumb equivalents and stay telemetry-only.
The pipeline's breadcrumb path simply omits them, with a diagnostic
marker. The classifier remains the same: any REJECT → rejected, any
WARN → usable_with_warnings, else clean.

Every :class:`BreadcrumbRule` produces a plain :class:`RuleResult` so
``classify()`` handles both rule streams without branching.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Mapping, Sequence

from src.replay.breadcrumbs import ReplayBreadcrumbs
from src.replay.rules.base import RuleResult, Severity

_RESPAWN_KIND = "Respawn"


class BreadcrumbRule(ABC):
    """Parallel ABC to :class:`src.replay.rules.base.Rule` but operating
    on :class:`ReplayBreadcrumbs`. Same threshold-merge semantics, same
    result type. Kept separate so the input type stays load-bearing in
    the signature — a rule declaring ``telemetry: ReplayTelemetry`` and
    another declaring ``breadcrumbs: ReplayBreadcrumbs`` can't be
    accidentally swapped by the runner.
    """

    name: ClassVar[str]
    version: ClassVar[str]
    default_thresholds: ClassVar[dict[str, Any]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for attr in ("name", "version", "default_thresholds"):
            if not hasattr(cls, attr):
                raise TypeError(
                    f"{cls.__name__} must declare class attribute '{attr}'"
                )

    def evaluate(
        self,
        breadcrumbs: ReplayBreadcrumbs,
        thresholds: Mapping[str, Any] | None = None,
    ) -> RuleResult:
        merged = dict(self.default_thresholds)
        if thresholds:
            merged.update(thresholds)
        return self._evaluate(breadcrumbs, merged)

    @abstractmethod
    def _evaluate(
        self,
        breadcrumbs: ReplayBreadcrumbs,
        thresholds: Mapping[str, Any],
    ) -> RuleResult:
        """Concrete evaluation. ``thresholds`` is defaults ∪ overrides."""

    def _pass(self, **evidence: Any) -> RuleResult:
        return RuleResult(
            rule_name=self.name,
            rule_version=self.version,
            passed=True,
            severity=None,
            evidence=dict(evidence),
        )

    def _warn(self, **evidence: Any) -> RuleResult:
        return RuleResult(
            rule_name=self.name,
            rule_version=self.version,
            passed=False,
            severity=Severity.WARN,
            evidence=dict(evidence),
        )

    def _reject(self, **evidence: Any) -> RuleResult:
        return RuleResult(
            rule_name=self.name,
            rule_version=self.version,
            passed=False,
            severity=Severity.REJECT,
            evidence=dict(evidence),
        )


class BreadcrumbIncompleteRule(BreadcrumbRule):
    """Replay ran far too briefly or never reached a finish.

    Breadcrumb signals: `finish_time_ms`, `checkpoint_times_ms`,
    `inputs_count`. Positional `min_samples` has no analog here — the
    closest analog is a minimum input count, since a fractional-second
    replay would have almost no events.
    """

    name = "breadcrumb_incomplete"
    version = "1.0.0"
    default_thresholds = {
        "min_inputs": 20,
        "min_duration_ms": 5_000,
    }

    def _evaluate(
        self, breadcrumbs: ReplayBreadcrumbs, thresholds: Mapping[str, Any]
    ) -> RuleResult:
        min_inputs = int(thresholds["min_inputs"])
        min_duration_ms = int(thresholds["min_duration_ms"])
        if breadcrumbs.inputs_count < min_inputs:
            return self._reject(
                reason="too_few_inputs",
                inputs_count=breadcrumbs.inputs_count,
                min_inputs=min_inputs,
            )
        duration_ms = breadcrumbs.duration_ms
        finished = breadcrumbs.finish_time_ms is not None
        if not finished and (duration_ms is None or duration_ms < min_duration_ms):
            return self._reject(
                reason="too_short_no_finish",
                duration_ms=duration_ms,
                min_duration_ms=min_duration_ms,
            )
        if not finished:
            return self._warn(
                reason="no_finish_event",
                duration_ms=duration_ms,
            )
        return self._pass(
            inputs_count=breadcrumbs.inputs_count,
            duration_ms=duration_ms,
            finish_time_ms=breadcrumbs.finish_time_ms,
        )


class BreadcrumbRestartRule(BreadcrumbRule):
    """Count Respawn input events as in-run restart evidence.

    Maps directly onto the existing :class:`RestartRule` thresholds;
    TM2020's `Respawn` IInput fires for both soft (checkpoint-restart)
    and hard (race-restart) resets, so the count is a superset of
    what the telemetry rule's ``restart_sample_indices`` would catch.
    Threshold defaults are the same to keep behavior aligned when the
    population shifts between telemetry and breadcrumb paths.
    """

    name = "breadcrumb_restart"
    version = "1.0.0"
    default_thresholds = {
        "warn_at_count": 1,
        "reject_at_count": 3,
    }

    def _evaluate(
        self, breadcrumbs: ReplayBreadcrumbs, thresholds: Mapping[str, Any]
    ) -> RuleResult:
        warn_at = int(thresholds["warn_at_count"])
        reject_at = int(thresholds["reject_at_count"])
        if warn_at > reject_at:
            raise ValueError("warn_at_count must be <= reject_at_count")
        count = breadcrumbs.count_inputs_by_kind(_RESPAWN_KIND)
        if count >= reject_at:
            return self._reject(
                reason="too_many_respawns",
                respawn_count=count,
                reject_at_count=reject_at,
            )
        if count >= warn_at:
            return self._warn(
                reason="respawn_events_present",
                respawn_count=count,
                warn_at_count=warn_at,
            )
        return self._pass(respawn_count=count)


class BreadcrumbSpectatorRule(BreadcrumbRule):
    """Low input density → probably not a real racing run.

    The telemetry spectator rule uses total path length (needs
    positions). Without positions, `inputs per second` is the closest
    cheap proxy for "someone was actually driving": a spectator-camera
    replay, paused menu capture, or mid-finish disconnect has few
    control events regardless of wall-clock duration. The default
    threshold is deliberately conservative (1 event/sec) so it rejects
    obvious non-driving-replays without false-flagging slow drivers.
    """

    name = "breadcrumb_spectator"
    version = "1.0.0"
    default_thresholds = {
        "min_inputs_per_second": 1.0,
    }

    def _evaluate(
        self, breadcrumbs: ReplayBreadcrumbs, thresholds: Mapping[str, Any]
    ) -> RuleResult:
        min_rate = float(thresholds["min_inputs_per_second"])
        duration_ms = breadcrumbs.duration_ms
        if duration_ms is None or duration_ms <= 0:
            # No race phase to measure density against — defer to the
            # incomplete rule which will reject on its own signals.
            return self._pass(reason="no_duration", inputs_count=breadcrumbs.inputs_count)
        rate = breadcrumbs.inputs_count / (duration_ms / 1000.0)
        if rate < min_rate:
            return self._reject(
                reason="low_input_density",
                inputs_per_second=round(rate, 4),
                min_inputs_per_second=min_rate,
                inputs_count=breadcrumbs.inputs_count,
                duration_ms=duration_ms,
            )
        return self._pass(
            inputs_per_second=round(rate, 4),
            inputs_count=breadcrumbs.inputs_count,
            duration_ms=duration_ms,
        )


class BreadcrumbInvalidTimingRule(BreadcrumbRule):
    """Checkpoint-sequence timing sanity.

    The telemetry equivalent looks at per-sample timestamps. Without
    samples, checkpoint-to-checkpoint gaps are the best time-ordered
    signal we have. Non-monotonic checkpoint times would be a hard
    parse bug (reject). Any gap wider than ``hard_gap_ms`` is a
    near-certainty reject; gaps above ``max_gap_factor`` × the median
    gap (but below the hard cap) warn — a slow segment isn't an
    invalid-timing fault by itself.
    """

    name = "breadcrumb_invalid_timing"
    version = "1.0.0"
    default_thresholds = {
        "max_gap_factor": 5.0,
        "hard_gap_ms": 120_000,  # 2 minutes between checkpoints; above this is very likely a paused replay
    }

    def _evaluate(
        self, breadcrumbs: ReplayBreadcrumbs, thresholds: Mapping[str, Any]
    ) -> RuleResult:
        max_factor = float(thresholds["max_gap_factor"])
        hard_gap_ms = int(thresholds["hard_gap_ms"])
        cps = breadcrumbs.checkpoint_times_ms
        if len(cps) < 2:
            return self._pass(
                reason="too_few_checkpoints_to_judge",
                checkpoint_count=len(cps),
            )
        gaps: list[int] = []
        backwards_index = -1
        for i in range(1, len(cps)):
            dt = cps[i] - cps[i - 1]
            if dt < 0 and backwards_index < 0:
                backwards_index = i
            gaps.append(dt)

        if backwards_index >= 0:
            return self._reject(
                reason="non_monotonic_checkpoints",
                at_checkpoint_index=backwards_index,
                delta_ms=cps[backwards_index] - cps[backwards_index - 1],
            )
        worst_gap_ms = max(gaps)
        worst_gap_index = gaps.index(worst_gap_ms) + 1
        sorted_gaps = sorted(gaps)
        median_gap_ms = sorted_gaps[len(sorted_gaps) // 2]
        soft_limit_ms = max_factor * median_gap_ms

        if worst_gap_ms > hard_gap_ms:
            return self._reject(
                reason="excessive_checkpoint_gap",
                worst_gap_ms=worst_gap_ms,
                at_checkpoint_index=worst_gap_index,
                hard_gap_ms=hard_gap_ms,
            )
        if worst_gap_ms > soft_limit_ms and median_gap_ms > 0:
            return self._warn(
                reason="gap_above_median",
                worst_gap_ms=worst_gap_ms,
                median_gap_ms=median_gap_ms,
                max_gap_factor=max_factor,
                at_checkpoint_index=worst_gap_index,
            )
        return self._pass(
            worst_gap_ms=worst_gap_ms,
            median_gap_ms=median_gap_ms,
        )


def default_breadcrumb_rules() -> list[BreadcrumbRule]:
    """Canonical order for breadcrumb rules. Order is cosmetic (the
    classifier treats rules as peers).
    """
    return [
        BreadcrumbIncompleteRule(),
        BreadcrumbInvalidTimingRule(),
        BreadcrumbRestartRule(),
        BreadcrumbSpectatorRule(),
    ]


def run_breadcrumb_rules(
    breadcrumbs: ReplayBreadcrumbs,
    rules: Sequence[BreadcrumbRule],
    thresholds_by_rule: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[RuleResult]:
    lookup: Mapping[str, Mapping[str, Any]] = thresholds_by_rule or {}
    return [rule.evaluate(breadcrumbs, lookup.get(rule.name)) for rule in rules]
