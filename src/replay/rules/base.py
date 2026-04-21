"""Rule framework for replay cleaning.

A Rule is a pure function of :class:`ReplayTelemetry` + a threshold
dict. Thresholds come from ``config.replay_cleaning.rules.<name>`` and
fall back to the rule's declared defaults. Each rule declares a
semver ``version``; bumping it is a schema-level change because
``replays.clean_version`` pins the exact versions used.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Mapping, Sequence

from src.replay.telemetry import ReplayTelemetry


class Severity(str, Enum):
    WARN = "warn"
    REJECT = "reject"


@dataclass(frozen=True)
class RuleResult:
    rule_name: str
    rule_version: str
    passed: bool
    severity: Severity | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.passed and self.severity is not None:
            raise ValueError("passed=True requires severity=None")
        if not self.passed and self.severity is None:
            raise ValueError("passed=False requires a Severity")


class Rule(ABC):
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
        telemetry: ReplayTelemetry,
        thresholds: Mapping[str, Any] | None = None,
    ) -> RuleResult:
        merged = dict(self.default_thresholds)
        if thresholds:
            merged.update(thresholds)
        return self._evaluate(telemetry, merged)

    @abstractmethod
    def _evaluate(
        self,
        telemetry: ReplayTelemetry,
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


def run_rules(
    telemetry: ReplayTelemetry,
    rules: Sequence[Rule],
    thresholds_by_rule: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[RuleResult]:
    """Evaluate each rule in order and return all outcomes."""
    lookup: Mapping[str, Mapping[str, Any]] = thresholds_by_rule or {}
    return [rule.evaluate(telemetry, lookup.get(rule.name)) for rule in rules]
