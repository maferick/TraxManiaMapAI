"""Behavior-profile evaluator.

Aggregates per-map driving-behavior features from the breadcrumb-path
rule evidence persisted on each replay's ``clean_diagnostics`` row.
Produces:

- ``flow_score`` — cross-replay consistency of input density. Defined
  as ``1 - clip(stdev / mean, 0, 1)`` over the ``inputs_per_second``
  values reported by :class:`BreadcrumbSpectatorRule` across all clean
  replays on the map. A value near 1.0 means every driver experienced
  a similar input intensity; a value near 0.0 means drivers disagreed
  strongly (one cruised, another was working hard). This is a measured
  dimension, NOT a quality claim — "consistent" ≠ "good."
- ``diagnostics`` — richer per-map aggregates (replay_count, respawn
  stats, duration spread, checkpoint-gap stats, etc.) so the dry-run
  report can surface multiple distributions at once.

Only reads replays whose ``clean_diagnostics.signal_source`` is
``"breadcrumbs"`` (the only path available today on TM2020). Maps with
fewer than ``min_replays`` clean breadcrumb-path replays emit no
scores — reporting ``None`` rather than fabricating a number from one
replay matches the no-invented-scores pattern the other evaluators use.

Non-goals (deliberate, follows ``docs/workstreams/corridor-inference.md``
anti-patterns):

- inferring map quality or difficulty from these features alone
- scoring "technical" / "flowing" / "jump-heavy" from input patterns
  (circular: the input pattern IS the signal)
- claiming flow_score separates quality cohorts without empirical
  evidence from the dry-run report
"""
from __future__ import annotations

import json
import math
import statistics
from typing import Any, Iterable

from pymysql.connections import Connection

from src.evaluation.base import Evaluator, EvaluationResult, utcnow
from src.evaluation.registry import register
from src.storage.mariadb import cursor
from src.utils.config import code_version

_DEFAULT_MIN_REPLAYS = 3
_BREADCRUMB_SIGNAL = "breadcrumbs"
_SPECTATOR_RULE = "breadcrumb_spectator"
_RESTART_RULE = "breadcrumb_restart"
_INCOMPLETE_RULE = "breadcrumb_incomplete"
_TIMING_RULE = "breadcrumb_invalid_timing"


def _extract_rule_evidence(diag: dict[str, Any], rule_name: str) -> dict[str, Any] | None:
    """Find the evidence dict for a named rule in the diagnostics
    payload, or None if the rule didn't run.
    """
    rules = diag.get("rules")
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if isinstance(rule, dict) and rule.get("name") == rule_name:
            ev = rule.get("evidence")
            return ev if isinstance(ev, dict) else None
    return None


def _coeff_of_variation(values: list[float]) -> float | None:
    """Standard-deviation / mean. Returns None when there are fewer
    than 2 values or the mean is non-positive (CV undefined).
    """
    if len(values) < 2:
        return None
    mean = statistics.mean(values)
    if mean <= 0:
        return None
    return statistics.stdev(values) / mean


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


@register
class BehaviorProfileEvaluator(Evaluator):
    name = "behavior_profile"
    version = "0.1.0"

    def __init__(
        self,
        conn: Connection,
        *,
        min_replays: int = _DEFAULT_MIN_REPLAYS,
    ) -> None:
        self._conn = conn
        self._min_replays = int(min_replays)

    def evaluate(
        self,
        map_id: int,
        *,
        benchmark_set_version: str | None = None,
    ) -> EvaluationResult:
        with cursor(self._conn) as cur:
            cur.execute(
                "SELECT id, clean_diagnostics FROM replays "
                "WHERE map_id = %s "
                "AND clean_status IN ('clean','usable_with_warnings') "
                "AND clean_diagnostics IS NOT NULL",
                (map_id,),
            )
            rows = cur.fetchall()

        inputs_per_second: list[float] = []
        respawn_counts: list[float] = []
        durations_ms: list[float] = []
        worst_gap_ms: list[float] = []
        median_gap_ms: list[float] = []
        per_replay_signal_sources: list[str] = []
        eligible_replay_ids: list[int] = []

        for rid, diag_json in rows:
            try:
                diag = json.loads(diag_json) if diag_json else {}
            except (TypeError, json.JSONDecodeError):
                continue
            signal_source = str(diag.get("signal_source", "")) or "unknown"
            per_replay_signal_sources.append(signal_source)
            if signal_source != _BREADCRUMB_SIGNAL:
                # Telemetry-path replays don't expose the same evidence
                # keys. Skip until we add a telemetry-branch in v0.2.
                continue
            spec_ev = _extract_rule_evidence(diag, _SPECTATOR_RULE)
            if spec_ev is None:
                continue
            ips = _as_float(spec_ev.get("inputs_per_second"))
            dur = _as_float(spec_ev.get("duration_ms"))
            rest_ev = _extract_rule_evidence(diag, _RESTART_RULE) or {}
            respawn = _as_float(rest_ev.get("respawn_count"))
            timing_ev = _extract_rule_evidence(diag, _TIMING_RULE) or {}
            worst = _as_float(timing_ev.get("worst_gap_ms"))
            median = _as_float(timing_ev.get("median_gap_ms"))

            if ips is None:
                continue
            inputs_per_second.append(ips)
            if dur is not None:
                durations_ms.append(dur)
            if respawn is not None:
                respawn_counts.append(respawn)
            if worst is not None:
                worst_gap_ms.append(worst)
            if median is not None:
                median_gap_ms.append(median)
            eligible_replay_ids.append(int(rid))

        diagnostics: dict[str, Any] = {
            "replay_count_total": len(rows),
            "replay_count_breadcrumb_eligible": len(eligible_replay_ids),
            "min_replays_required": self._min_replays,
            "signal_sources_observed": sorted(set(per_replay_signal_sources)),
        }

        if len(eligible_replay_ids) < self._min_replays:
            diagnostics["reason"] = "insufficient_breadcrumb_replays"
            return self._result(
                map_id=map_id,
                benchmark_set_version=benchmark_set_version,
                flow_score=None,
                diagnostics=diagnostics,
            )

        # Primary score: cross-replay input-density consistency.
        cv = _coeff_of_variation(inputs_per_second)
        if cv is None:
            flow_score: float | None = None
            diagnostics["flow_score_reason"] = "cv_undefined"
        else:
            # Clip CV into [0, 1] before inversion. A CV above 1.0 means
            # stdev exceeds the mean — extreme disagreement across
            # drivers; we floor it there rather than letting the score
            # go negative.
            flow_score = max(0.0, 1.0 - min(cv, 1.0))

        diagnostics.update(
            {
                "inputs_per_second_mean": round(statistics.mean(inputs_per_second), 4),
                "inputs_per_second_stdev": (
                    round(statistics.stdev(inputs_per_second), 4)
                    if len(inputs_per_second) >= 2 else 0.0
                ),
                "inputs_per_second_cv": (round(cv, 4) if cv is not None else None),
            }
        )
        if durations_ms:
            diagnostics["duration_ms_median"] = round(statistics.median(durations_ms))
            if len(durations_ms) >= 2:
                diagnostics["duration_ms_stdev"] = round(statistics.stdev(durations_ms))
        if respawn_counts:
            diagnostics["respawn_count_mean"] = round(statistics.mean(respawn_counts), 4)
            diagnostics["respawn_count_max"] = max(respawn_counts)
        if worst_gap_ms:
            diagnostics["checkpoint_worst_gap_ms_median"] = round(
                statistics.median(worst_gap_ms)
            )
        if median_gap_ms:
            diagnostics["checkpoint_median_gap_ms_median"] = round(
                statistics.median(median_gap_ms)
            )

        return self._result(
            map_id=map_id,
            benchmark_set_version=benchmark_set_version,
            flow_score=flow_score,
            diagnostics=diagnostics,
            source_replay_ids=eligible_replay_ids,
        )

    def _result(
        self,
        *,
        map_id: int,
        benchmark_set_version: str | None,
        flow_score: float | None,
        diagnostics: dict[str, Any],
        source_replay_ids: Iterable[int] = (),
    ) -> EvaluationResult:
        source_ids = {"map": str(map_id)}
        rid_list = list(source_replay_ids)
        if rid_list:
            # Cap serialized list to avoid blowing up the JSON column on
            # pathologically large replay corpora.
            source_ids["replays"] = ",".join(str(r) for r in rid_list[:64])
            source_ids["replay_count"] = str(len(rid_list))
        return EvaluationResult(
            map_id=map_id,
            evaluator_name=self.name,
            evaluator_version=self.version,
            benchmark_set_version=benchmark_set_version,
            created_at=utcnow(),
            code_version=code_version(),
            source_artifact_ids=source_ids,
            flow_score=flow_score,
            diagnostics=diagnostics,
        )
