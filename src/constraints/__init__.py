"""Constraint graph subsystem (PR 6).

See docs/architecture.md and src/constraints/README.md for the
subsystem overview. Core principle: **frequency is not validity**.
"""
from src.constraints.evidence import (
    ALL_LABELS,
    SUSPICIOUS,
    UNKNOWN,
    VALID,
    derive_validity_label,
)
from src.constraints.extractor import (
    extract_adjacencies,
    unique_block_keys,
)
from src.constraints.nodes import (
    AdjacencyEdge,
    AdjacencyObservation,
    BlockKey,
    order_pair,
)
from src.constraints.pipeline import BuildStats, ConstraintGraphPipeline

__all__ = [
    "ALL_LABELS",
    "AdjacencyEdge",
    "AdjacencyObservation",
    "BlockKey",
    "BuildStats",
    "ConstraintGraphPipeline",
    "SUSPICIOUS",
    "UNKNOWN",
    "VALID",
    "derive_validity_label",
    "extract_adjacencies",
    "order_pair",
    "unique_block_keys",
]
