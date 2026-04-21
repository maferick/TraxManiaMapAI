"""Replay cleaning + cohort assignment.

See docs/architecture.md for the subsystem overview and
src/replay/README.md for the on-ramp.
"""
from src.replay.classify import ClassificationOutcome, classify
from src.replay.cohorts import (
    CohortAssignment,
    CohortAssignmentConfig,
    MapCohortStats,
    assign_cohorts_for_map,
    summarize,
)
from src.replay.pipeline import (
    CleanStats,
    CohortAssignmentPipeline,
    CohortStats,
    FileTelemetryLoader,
    ReplayCleanPipeline,
    ReplayRow,
    TelemetryLoader,
    TelemetryLoadError,
)
from src.replay.rules import (
    IncompleteRule,
    InvalidTimingRule,
    OutlierSpeedRule,
    RestartRule,
    Rule,
    RuleResult,
    Severity,
    SpectatorRule,
    TeleportRule,
    ZeroMotionRule,
    default_rules,
    run_rules,
)
from src.replay.telemetry import (
    TELEMETRY_SCHEMA_VERSION,
    ReplayTelemetry,
    SampleFrame,
    TelemetryFormatError,
    from_dict,
)

__all__ = [
    "CleanStats",
    "ClassificationOutcome",
    "CohortAssignment",
    "CohortAssignmentConfig",
    "CohortAssignmentPipeline",
    "CohortStats",
    "FileTelemetryLoader",
    "IncompleteRule",
    "InvalidTimingRule",
    "MapCohortStats",
    "OutlierSpeedRule",
    "ReplayCleanPipeline",
    "ReplayRow",
    "ReplayTelemetry",
    "RestartRule",
    "Rule",
    "RuleResult",
    "SampleFrame",
    "Severity",
    "SpectatorRule",
    "TELEMETRY_SCHEMA_VERSION",
    "TelemetryFormatError",
    "TelemetryLoadError",
    "TelemetryLoader",
    "TeleportRule",
    "ZeroMotionRule",
    "assign_cohorts_for_map",
    "classify",
    "default_rules",
    "from_dict",
    "run_rules",
    "summarize",
]
