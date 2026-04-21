from __future__ import annotations

from src.replay.classify import classify
from src.replay.rules.base import RuleResult, Severity
from src.schema.replays import CleanStatus


def _pass(name: str = "r") -> RuleResult:
    return RuleResult(rule_name=name, rule_version="1.0.0", passed=True)


def _warn(name: str = "r", **ev) -> RuleResult:
    return RuleResult(
        rule_name=name, rule_version="1.0.0", passed=False, severity=Severity.WARN, evidence=ev
    )


def _reject(name: str = "r", **ev) -> RuleResult:
    return RuleResult(
        rule_name=name, rule_version="1.0.0", passed=False, severity=Severity.REJECT, evidence=ev
    )


def test_all_pass_is_clean() -> None:
    o = classify([_pass("a"), _pass("b")])
    assert o.status is CleanStatus.CLEAN
    assert o.triggered_rules == ()


def test_any_warn_is_usable() -> None:
    o = classify([_pass("a"), _warn("b")])
    assert o.status is CleanStatus.USABLE_WITH_WARNINGS
    assert o.triggered_rules == ("b",)


def test_any_reject_trumps_warn() -> None:
    o = classify([_warn("a"), _reject("b", reason="teleport")])
    assert o.status is CleanStatus.REJECTED
    assert "b" in o.triggered_rules
    assert o.rejection_reasons == ("b:teleport",)


def test_diagnostics_payload_has_all_rules() -> None:
    o = classify([_pass("a"), _warn("b", detail=1), _reject("c", reason="x")])
    payload = o.diagnostics_payload()
    assert [r["name"] for r in payload["rules"]] == ["a", "b", "c"]
    assert payload["status"] == "rejected"
    assert payload["triggered"] == ["b", "c"]


def test_no_reason_in_rejection_falls_back_to_name() -> None:
    o = classify([_reject("rule_x")])
    assert o.rejection_reasons == ("rule_x",)
