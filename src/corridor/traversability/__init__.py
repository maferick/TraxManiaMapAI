"""Traversability subgraph construction for corridor inference.

See ``docs/workstreams/corridor-prereq-2-traversability.md`` for the
design contract and Phase 1 commit bar.
"""
from src.corridor.traversability.classification import (
    AMBIGUOUS_FAMILIES,
    DRIVABLE_FAMILIES,
    FamilyBucket,
    NON_DRIVABLE_FAMILIES,
    classify_family,
)

__all__ = [
    "AMBIGUOUS_FAMILIES",
    "DRIVABLE_FAMILIES",
    "FamilyBucket",
    "NON_DRIVABLE_FAMILIES",
    "classify_family",
]
