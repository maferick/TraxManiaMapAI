"""Aggregate rule outcomes into a :class:`CleanStatus`.

Status rule:
    any REJECT  -> rejected
    any WARN    -> usable_with_warnings
    else        -> clean
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from src.replay.rules.base import RuleResult, Severity
from src.schema.replays import CleanStatus


@dataclass(frozen=True)
class ClassificationOutcome:
    status: CleanStatus
    triggered_rules: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    rule_results: tuple[RuleResult, ...]

    def diagnostics_payload(self) -> dict[str, Any]:
        """Serialize for storage in ``replays.clean_diagnostics``."""
        return {
            "status": self.status.value,
            "triggered": list(self.triggered_rules),
            "rejection_reasons": list(self.rejection_reasons),
            "rules": [
                {
                    "name": r.rule_name,
                    "version": r.rule_version,
                    "passed": r.passed,
                    "severity": r.severity.value if r.severity else None,
                    "evidence": r.evidence,
                }
                for r in self.rule_results
            ],
        }


def classify(results: Sequence[RuleResult]) -> ClassificationOutcome:
    triggered: list[str] = []
    rejection_reasons: list[str] = []
    any_reject = False
    any_warn = False

    for r in results:
        if r.passed:
            continue
        triggered.append(r.rule_name)
        if r.severity is Severity.REJECT:
            any_reject = True
            reason = r.evidence.get("reason") if isinstance(r.evidence, dict) else None
            rejection_reasons.append(f"{r.rule_name}:{reason}" if reason else r.rule_name)
        elif r.severity is Severity.WARN:
            any_warn = True

    if any_reject:
        status = CleanStatus.REJECTED
    elif any_warn:
        status = CleanStatus.USABLE_WITH_WARNINGS
    else:
        status = CleanStatus.CLEAN

    return ClassificationOutcome(
        status=status,
        triggered_rules=tuple(triggered),
        rejection_reasons=tuple(rejection_reasons),
        rule_results=tuple(results),
    )
