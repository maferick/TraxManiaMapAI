"""Generation types — dataclasses + enum shapes used across the
assembly and finishability modules.

Kept deliberately small and immutable. Anything that's not shape is
implemented in :mod:`src.generation.assembly` or
:mod:`src.generation.finishability` — this file is where a reviewer
looks to understand the nouns, not the verbs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# Canonical reject-reason enumeration. Mirrors scope-v0 §Finishability
# semantics → reject_reason table. Adding a new value requires a
# scope-doc revision bump.
RejectReason = Literal[
    "plain_cp_not_supported_v0",
    "missing_corridor_in_interval",
    "chain_broken",
    "empty_corridors",
    "confidence_below_floor",
    "unknown_block",
    "invalid_schema",
    "stripped_route_broken",
]


# Canonical cell type — same shape route_corridors.path_cells serialises.
Cell = tuple[int, int, int]


@dataclass(frozen=True)
class Anchor:
    """One endpoint in the Spawn → CP₁ → CP₂ → … → Goal chain.

    ``order`` matches ``map_checkpoints.waypoint_order`` for CPs and
    is conventionally 0 for Spawn/Goal. ``cell`` is the cell the
    anchor block occupies, used to validate chain continuity across
    interval boundaries."""
    tag: str
    order: int
    cell: Cell | None = None        # None when we don't have placement info


@dataclass(frozen=True)
class ChosenCorridor:
    """The corridor picked to fulfil one interval.

    Mirrors the subset of ``route_corridors`` columns the assembly
    algorithm needs plus the derived per-corridor expected time.
    ``learned_corridor_score`` is the primary ranking signal used for
    selection and later aggregated into the route's AI confidence.
    ``combined_sequence_score`` is the #218 pattern+geometry score,
    present when the corridor was scored by
    :func:`src.constraints.sequence_scoring.score_all_corridors`;
    None otherwise. Purely a tier-below tie-break for assembly and
    a diagnostic field in the artifact; never bypasses the
    finishability gate."""
    corridor_id: int
    map_id: int
    src: Anchor
    dst: Anchor
    path_cells: tuple[Cell, ...]
    path_length: int
    contains_virtual_edge: bool
    corridor_confidence: float | None
    learned_corridor_score: float
    expected_time_ms: int
    combined_sequence_score: float | None = None


@dataclass(frozen=True)
class IntervalAssembly:
    """One step in the route. Matches the ``route.intervals[*]`` entry
    shape in the generated-map JSON artifact (scope-v0 §Interval entry
    shape)."""
    index: int
    src: Anchor
    dst: Anchor
    chosen: ChosenCorridor


@dataclass(frozen=True)
class AssembledRoute:
    """Complete Spawn→Goal chain produced by :func:`assemble_route`.

    If assembly can't produce one (plain-CP map, missing corridor on
    some interval, chain discontinuity), the function returns an
    :class:`AssemblyError` instead — never a partial route with a
    silent gap."""
    map_id: int
    anchors: tuple[Anchor, ...]
    intervals: tuple[IntervalAssembly, ...]
    cells_total: int
    estimated_time_ms: int
    ai_confidence: float


@dataclass(frozen=True)
class AssemblyError:
    """Structured failure from :func:`assemble_route`. The calling
    finishability gate translates ``reason`` into the artifact's
    ``reject_reason`` string."""
    reason: RejectReason
    detail: str                     # human-readable context, safe to surface in UI
    interval_index: int | None = None


@dataclass(frozen=True)
class FinishabilityResult:
    """What :func:`run_finishability_gate` emits. Mirrors the
    ``finishability`` block of the generated-map JSON artifact so
    serialization is a 1:1 projection."""
    route_verified: bool
    estimated_time_ms: int | None
    ai_confidence: float | None
    reject_reason: RejectReason | None
    gate_version: str
    detail: str | None = None       # reason detail when route_verified=False
