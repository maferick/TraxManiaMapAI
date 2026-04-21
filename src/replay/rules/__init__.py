"""Cleaning rules + the default registry.

The default rule order is the order below. The classifier treats all
rules as peers — a rule earlier in the list is not preferred — so
the order is mainly about how diagnostics read.
"""
from __future__ import annotations

from src.replay.rules.base import Rule, RuleResult, Severity, run_rules
from src.replay.rules.incomplete import IncompleteRule
from src.replay.rules.invalid_timing import InvalidTimingRule
from src.replay.rules.outlier_speed import OutlierSpeedRule
from src.replay.rules.restart import RestartRule
from src.replay.rules.spectator import SpectatorRule
from src.replay.rules.teleport import TeleportRule
from src.replay.rules.zero_motion import ZeroMotionRule


def default_rules() -> list[Rule]:
    """A fresh list of rule singletons in canonical order."""
    return [
        IncompleteRule(),
        InvalidTimingRule(),
        TeleportRule(),
        OutlierSpeedRule(),
        ZeroMotionRule(),
        RestartRule(),
        SpectatorRule(),
    ]


__all__ = [
    "IncompleteRule",
    "InvalidTimingRule",
    "OutlierSpeedRule",
    "RestartRule",
    "Rule",
    "RuleResult",
    "Severity",
    "SpectatorRule",
    "TeleportRule",
    "ZeroMotionRule",
    "default_rules",
    "run_rules",
]
