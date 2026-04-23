"""Finishability gate per ``docs/generation/generation-scope-v0.md`` §
Finishability semantics.

One entry point: :func:`run_finishability_gate`. Takes an
:class:`AssembledRoute` or :class:`AssemblyError` and produces a
:class:`FinishabilityResult` — the shape that matches the
``finishability`` block of the generated-map JSON artifact 1:1.

Narrow by design. The scope doc explicitly says: "The gate's role
is narrow: 'given the current route_corridors table, does a chain
close?' Yes or no. It doesn't second-guess the corridor data." The
only thing this module does beyond translating the assembly result
is apply the ``ai_confidence`` floor.
"""
from __future__ import annotations

from src.generation.types import (
    AssembledRoute,
    AssemblyError,
    FinishabilityResult,
)


# Gate identity. Scope-v0 pins this literal; consumers can assume the
# reject_reason enumeration is exhaustive for this version.
GATE_VERSION: str = "finishability-v0"


# Sanity floor per scope-v0: "below this, the model isn't committing
# enough to back 'yes, this route is finishable' even if the chain
# closed."
AI_CONFIDENCE_FLOOR: float = 0.30


def run_finishability_gate(
    result: AssembledRoute | AssemblyError,
) -> FinishabilityResult:
    """Translate an assembly result into the operator-facing verdict.

    Three cases:

    - Assembly returned an :class:`AssemblyError` → route_verified=False,
      reject_reason + detail come straight from the error. No numeric
      fields populated (estimated_time and ai_confidence both None)
      so the UI doesn't show stale numbers next to a reject.

    - Assembly returned a route with ai_confidence below
      :data:`AI_CONFIDENCE_FLOOR` → route_verified=False,
      reject_reason="confidence_below_floor", but DO populate the
      numeric fields so the operator can see what the numbers were.
      This helps decide whether to retrain vs widen the corpus.

    - Assembly returned a clean route at or above the floor →
      route_verified=True, everything populated.
    """
    if isinstance(result, AssemblyError):
        return FinishabilityResult(
            route_verified=False,
            estimated_time_ms=None,
            ai_confidence=None,
            reject_reason=result.reason,
            gate_version=GATE_VERSION,
            detail=result.detail,
        )

    # route is an AssembledRoute.
    if result.ai_confidence < AI_CONFIDENCE_FLOOR:
        return FinishabilityResult(
            route_verified=False,
            estimated_time_ms=result.estimated_time_ms,
            ai_confidence=result.ai_confidence,
            reject_reason="confidence_below_floor",
            gate_version=GATE_VERSION,
            detail=(
                f"ai_confidence {result.ai_confidence:.3f} below floor "
                f"{AI_CONFIDENCE_FLOOR:.2f}"
            ),
        )

    return FinishabilityResult(
        route_verified=True,
        estimated_time_ms=result.estimated_time_ms,
        ai_confidence=result.ai_confidence,
        reject_reason=None,
        gate_version=GATE_VERSION,
        detail=None,
    )
