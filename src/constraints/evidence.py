"""Validity-label policy for adjacency edges.

The core rule (``CLAUDE.md``, ``docs/evaluation-plan.md``):
**frequency is NOT validity**. An edge that appears in 10,000 maps
with no other evidence is still ``unknown``. An edge appearing once
in a benchmark-strong map is ``valid``.

Invariants enforced here:

1. ``benchmark_strong_count >= 1``  → ``"valid"``
2. ``broken_fixture_count > 0`` AND no positive evidence → ``"suspicious"``
3. everything else (including very high ``observed_in_maps_count``) → ``"unknown"``

``replay_supported_count`` currently does NOT upgrade an edge to
``valid`` on its own — replay support is an *observation* of routing
behavior, not proof of validity. Once the replay-to-block projection
lands in a later PR and we can distinguish clean-cohort replays from
rejected ones, we may tighten this to let clean-cohort replays
contribute to validity. Until then, keep the policy deliberately
conservative.
"""
from __future__ import annotations

VALID = "valid"
SUSPICIOUS = "suspicious"
UNKNOWN = "unknown"

ALL_LABELS: frozenset[str] = frozenset({VALID, SUSPICIOUS, UNKNOWN})


def derive_validity_label(
    *,
    benchmark_strong_count: int,
    broken_fixture_count: int,
    replay_supported_count: int,
    observed_in_maps_count: int,
) -> str:
    """Pure function — no side effects, no DB reads. Same inputs → same output.

    ``observed_in_maps_count`` is accepted so the signature is complete
    and callers can't forget to pass it, but it is **intentionally
    unused** by the policy. A frequency-based rule would fail the
    "no frequency-as-validity" invariant from ``docs/evaluation-plan.md``.
    """
    del observed_in_maps_count  # documented: frequency does not decide validity

    for name, value in (
        ("benchmark_strong_count", benchmark_strong_count),
        ("broken_fixture_count", broken_fixture_count),
        ("replay_supported_count", replay_supported_count),
    ):
        if value < 0:
            raise ValueError(f"{name} must be >= 0, got {value}")

    if benchmark_strong_count >= 1:
        return VALID
    if (
        broken_fixture_count > 0
        and benchmark_strong_count == 0
        and replay_supported_count == 0
    ):
        return SUSPICIOUS
    return UNKNOWN
