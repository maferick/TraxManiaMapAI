"""Phase 2 generation scaffold.

Per ``docs/generation/generation-scope-v0.md`` — this PR ships the
pure-function scaffold only:

- :func:`assemble_route` — enumerate Linked-CP anchors, pick top
  learned-score corridor per interval, check chain continuity.
- :func:`run_finishability_gate` — apply confidence floor, stamp
  gate version, produce a :class:`FinishabilityResult`.

No CLI, no generator — those are PR E / PR F. This module's only
job is to host the canonical algorithms the scope doc pins, with
unit tests, so later PRs reuse one implementation.

The reject-reason enumeration matches the scope doc exactly. Any
new failure mode requires a doc-revision bump (``scope-v0.1.md``).
"""
from src.generation.assembly import (
    AssemblyInputs,
    CandidateCorridor,
    assemble_route,
    assemble_route_from_inputs,
)
from src.generation.finishability import (
    GATE_VERSION,
    AI_CONFIDENCE_FLOOR,
    run_finishability_gate,
)
from src.generation.generator import GenerationInputs, generate_from_base
from src.generation.schema import load_schema, validate_generated_map
from src.generation.types import (
    Anchor,
    AssembledRoute,
    AssemblyError,
    ChosenCorridor,
    FinishabilityResult,
    IntervalAssembly,
    RejectReason,
)

__all__ = [
    "AI_CONFIDENCE_FLOOR",
    "Anchor",
    "AssembledRoute",
    "AssemblyError",
    "AssemblyInputs",
    "CandidateCorridor",
    "ChosenCorridor",
    "FinishabilityResult",
    "GATE_VERSION",
    "GenerationInputs",
    "IntervalAssembly",
    "RejectReason",
    "assemble_route",
    "assemble_route_from_inputs",
    "generate_from_base",
    "load_schema",
    "run_finishability_gate",
    "validate_generated_map",
]
