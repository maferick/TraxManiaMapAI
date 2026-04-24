"""Pre-emit validator integration (follow-up to #226 / #227).

Runs :mod:`src.generation.geom_validator` and
:mod:`src.generation.jump_validator` against a freshly-assembled
route (optionally strip-projected) and aggregates the findings into
a single :class:`PreEmitValidationSummary`.

The summary is:
  - logged from :func:`src.generation.generator.generate_from_base`
    at INFO level (so `_cmd_generate_map` output surfaces counts),
  - available as a pure-function call for callers that want to
    drive the validators themselves (the CLI's `validate-generation`
    command and unit tests).

Scope boundary (CLAUDE.md):
  - never mutates the artifact,
  - never raises on findings,
  - never talks to the finishability gate,
  - pure when :func:`run_preemit_validation` is called with a
    pre-loaded ``geometry_lookup`` (DB access isolated to the loader).

The artifact JSON schema stays untouched; this module's summary
lives next to the artifact as a sidecar (written by the CLI layer)
rather than extending the schema.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from src.generation.geom_validator import (
    CODE_MISSING_SUPPORT,
    CODE_PARTIAL_MULTICELL,
    CODE_ROUTE_CELL_MISSING_BLOCK,
    CODE_ROUTE_GAP,
    CODE_SPAWN_INTERSECT,
    Cell,
    Finding,
    GeometryInfo,
    SEVERITY_FAIL,
    SEVERITY_INFO,
    SEVERITY_WARN,
    _chebyshev,
    validate_map_geometry,
)
from src.generation.jump_validator import (
    CLASS_GEOMETRICALLY_PLAUSIBLE,
    CLASS_LIKELY_BROKEN,
    CLASS_SUPPORTED_BY_REPLAY,
    CLASS_UNCERTAIN,
    JumpConeConfig,
    JumpReport,
    validate_jumps,
)

_LOG = logging.getLogger(__name__)

# Version tag recorded on every validation summary so a dashboard can
# filter out older/incompatible summaries after a check set changes.
PREEMIT_VERSION: str = "preemit-v0"


@dataclass(frozen=True)
class CorridorValidationScore:
    """Per-corridor validator derivative — a soft scoring signal.

    Not a replacement for learned_corridor_score or
    combined_sequence_score. An additional signal whose primary
    purpose is telemetry + future-ranking input: post-hoc analysis
    can correlate low ``validation_score`` with real in-game failures
    to tune the formula.

    Score interpretation: ``1.0`` = clean (no findings near this
    corridor's path); ``0.0`` = maximum penalty. Formula is a soft
    subtract, documented in :func:`_corridor_validation_score`.
    """
    corridor_id: int
    interval_index: int
    path_length: int
    partial_multicell_hits: int      # shadow cells empty near path
    missing_support_hits: int
    route_gap_hits: int
    jump_likely_broken: int
    jump_uncertain: int
    jump_geometrically_plausible: int
    jump_supported_by_replay: int
    validation_score: float          # in [0, 1]; higher = cleaner


@dataclass(frozen=True)
class PreEmitValidationSummary:
    """Aggregated validator output attached to a generation run."""
    version: str
    fail_count: int
    warn_count: int
    info_count: int
    code_counts: Mapping[str, int]
    jump_class_counts: Mapping[str, int]
    top_findings: tuple[Finding, ...]          # capped to _TOP_N
    blocks_total: int
    grid_blocks_total: int
    route_cells_total: int
    per_corridor_scores: tuple[CorridorValidationScore, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form — safe for sidecar / dashboard use."""
        return {
            "version": self.version,
            "fail_count": self.fail_count,
            "warn_count": self.warn_count,
            "info_count": self.info_count,
            "code_counts": dict(self.code_counts),
            "jump_class_counts": dict(self.jump_class_counts),
            "top_findings": [
                {
                    "severity": f.severity,
                    "code": f.code,
                    "detail": f.detail,
                    "cell": list(f.cell) if f.cell is not None else None,
                    "block": (
                        {
                            "family": f.block.family,
                            "name": f.block.name,
                            "cell": list(f.block.cell),
                        } if f.block is not None else None
                    ),
                }
                for f in self.top_findings
            ],
            "blocks_total": self.blocks_total,
            "grid_blocks_total": self.grid_blocks_total,
            "route_cells_total": self.route_cells_total,
            "per_corridor_scores": [
                {
                    "corridor_id": s.corridor_id,
                    "interval_index": s.interval_index,
                    "path_length": s.path_length,
                    "partial_multicell_hits": s.partial_multicell_hits,
                    "missing_support_hits": s.missing_support_hits,
                    "route_gap_hits": s.route_gap_hits,
                    "jump_likely_broken": s.jump_likely_broken,
                    "jump_uncertain": s.jump_uncertain,
                    "jump_geometrically_plausible": (
                        s.jump_geometrically_plausible
                    ),
                    "jump_supported_by_replay": s.jump_supported_by_replay,
                    "validation_score": s.validation_score,
                }
                for s in self.per_corridor_scores
            ],
        }


# Number of findings to surface in the summary. Full lists may be
# large; we want the summary compact enough to attach to artifacts
# without bloating logs. Consumers wanting the full list re-run the
# validators themselves.
_TOP_N = 20


def _normalize_block(block: Mapping[str, Any]) -> dict[str, Any]:
    """Translate DB-shaped block rows (``block_family``/``block_name``)
    into the parsed-GBX wrapper shape (``family``/``name``) the
    validators consume. Grid blocks keep their x/y/z and rotation.
    """
    return {
        "placement": "grid",
        "x": int(block.get("x", 0)),
        "y": int(block.get("y", 0)),
        "z": int(block.get("z", 0)),
        "rotation": int(block.get("rotation", 0)),
        "family": str(block.get("block_family") or block.get("family") or ""),
        "name": str(block.get("block_name") or block.get("name") or ""),
    }


# ---------------------------------------------------------------------
# Per-corridor scoring — soft signal (#226 / #227 follow-up).
# ---------------------------------------------------------------------

# Penalty weights. Deliberately conservative — the validator is a
# soft signal, not a veto. A ``likely_broken`` jump costs roughly
# the same as 4 partial-multicell hits; it's a strong negative but
# doesn't take a corridor from a 1.0 score to 0.0 on its own.
_W_PARTIAL_MULTICELL: float = 0.08
_W_MISSING_SUPPORT: float = 0.04
_W_ROUTE_GAP: float = 0.05
_W_JUMP_LIKELY_BROKEN: float = 0.30
_W_JUMP_UNCERTAIN: float = 0.08
# Proximity radius (Chebyshev) for "this finding is 'near' the
# corridor's path". Findings outside the radius are counted
# map-globally but not attributed to any one corridor.
_NEAR_RADIUS: int = 3


def _corridor_validation_score(
    *,
    partial_multicell_hits: int,
    missing_support_hits: int,
    route_gap_hits: int,
    jump_likely_broken: int,
    jump_uncertain: int,
) -> float:
    """Map per-corridor finding counts to a score in [0, 1].

    Formula: clamp(1 − Σ(w_i × count_i), 0, 1). Clipped at 0 so a
    deeply-broken corridor doesn't drive the score negative. Clipped
    at 1 so we don't flirt with Σ = 0 edge cases.
    """
    penalty = (
        _W_PARTIAL_MULTICELL * partial_multicell_hits
        + _W_MISSING_SUPPORT * missing_support_hits
        + _W_ROUTE_GAP * route_gap_hits
        + _W_JUMP_LIKELY_BROKEN * jump_likely_broken
        + _W_JUMP_UNCERTAIN * jump_uncertain
    )
    return max(0.0, min(1.0, 1.0 - penalty))


def _near(path_cells: set[Cell], finding_cell: Cell | None) -> bool:
    if finding_cell is None:
        return False
    for pc in path_cells:
        if _chebyshev(finding_cell, pc) <= _NEAR_RADIUS:
            return True
    return False


def _top_findings(findings: list[Finding]) -> tuple[Finding, ...]:
    # Severity-ordered: FAIL first, then WARN, then INFO. Within each
    # bucket the order matches call order (deterministic for tests).
    buckets: dict[str, list[Finding]] = {
        SEVERITY_FAIL: [], SEVERITY_WARN: [], SEVERITY_INFO: [],
    }
    for f in findings:
        buckets.setdefault(f.severity, []).append(f)
    ordered: list[Finding] = []
    for sev in (SEVERITY_FAIL, SEVERITY_WARN, SEVERITY_INFO):
        ordered.extend(buckets.get(sev, []))
    return tuple(ordered[:_TOP_N])


def run_preemit_validation(
    *,
    blocks: Iterable[Mapping[str, Any]],
    geometry_lookup: Mapping[tuple[str, str], GeometryInfo],
    route_cells: Iterable[Cell] | None = None,
    spawn_cell: Cell | None = None,
    replay_touched_cells: set[Cell] | None = None,
    ground_y: int = 9,
    max_route_step_cheb: int = 1,
    jump_cone: JumpConeConfig | None = None,
    corridor_paths: (
        Iterable[tuple[int, int, Iterable[Cell]]] | None
    ) = None,
) -> PreEmitValidationSummary:
    """Run structural + jump validators and aggregate findings.

    ``blocks`` may arrive in either the DB row shape
    (``block_family`` / ``block_name``) or the parsed-GBX wrapper
    shape (``family`` / ``name``); both are normalised before
    validation so callers don't have to remember which.

    ``corridor_paths`` — optional iterable of
    ``(corridor_id, interval_index, path_cells)`` tuples. When
    supplied, each corridor gets a per-corridor
    :class:`CorridorValidationScore` in the returned summary. The
    score attributes findings to a corridor when the finding's cell
    lies within Chebyshev radius 3 of the corridor's path. Purely a
    telemetry signal — the assembly tie-break is unchanged.
    """
    normalised = [_normalize_block(b) for b in blocks]
    route_list = list(route_cells) if route_cells is not None else []

    geom_report = validate_map_geometry(
        blocks=normalised,
        geometry_lookup=geometry_lookup,
        route_cells=route_list,
        spawn_cell=spawn_cell,
        ground_y=ground_y,
        max_route_step_cheb=max_route_step_cheb,
    )

    jump_report: JumpReport | None = None
    if route_list:
        jump_report = validate_jumps(
            blocks=normalised,
            geometry_lookup=geometry_lookup,
            route_cells=route_list,
            replay_touched_cells=replay_touched_cells,
            cone=jump_cone,
        )

    all_findings: list[Finding] = list(geom_report.findings)
    jump_class_counts: dict[str, int] = {}
    if jump_report is not None:
        all_findings.extend(jump_report.findings())
        for cls in (
            CLASS_SUPPORTED_BY_REPLAY,
            CLASS_GEOMETRICALLY_PLAUSIBLE,
            CLASS_UNCERTAIN,
            CLASS_LIKELY_BROKEN,
        ):
            jump_class_counts[cls] = len(jump_report.by_class(cls))

    code_counts: dict[str, int] = {}
    for f in all_findings:
        code_counts[f.code] = code_counts.get(f.code, 0) + 1

    fail_count = sum(1 for f in all_findings if f.severity == SEVERITY_FAIL)
    warn_count = sum(1 for f in all_findings if f.severity == SEVERITY_WARN)
    info_count = sum(1 for f in all_findings if f.severity == SEVERITY_INFO)

    per_corridor_scores: tuple[CorridorValidationScore, ...] = ()
    if corridor_paths is not None:
        per_corridor_scores = _score_per_corridor(
            corridor_paths=corridor_paths,
            normalised_blocks=normalised,
            geometry_lookup=geometry_lookup,
            replay_touched_cells=replay_touched_cells,
            ground_y=ground_y,
            max_route_step_cheb=max_route_step_cheb,
            jump_cone=jump_cone,
        )

    summary = PreEmitValidationSummary(
        version=PREEMIT_VERSION,
        fail_count=fail_count,
        warn_count=warn_count,
        info_count=info_count,
        code_counts=code_counts,
        jump_class_counts=jump_class_counts,
        top_findings=_top_findings(all_findings),
        blocks_total=geom_report.blocks_total,
        grid_blocks_total=geom_report.grid_blocks_total,
        route_cells_total=geom_report.route_cells_total,
        per_corridor_scores=per_corridor_scores,
    )

    _LOG.info(
        "run_preemit_validation: blocks=%d route_cells=%d "
        "fail=%d warn=%d info=%d codes=%s jumps=%s",
        summary.blocks_total, summary.route_cells_total,
        summary.fail_count, summary.warn_count, summary.info_count,
        _compact_counts(summary.code_counts),
        _compact_counts(summary.jump_class_counts),
    )
    return summary


def _score_per_corridor(
    *,
    corridor_paths: Iterable[tuple[int, int, Iterable[Cell]]],
    normalised_blocks: list[Mapping[str, Any]],
    geometry_lookup: Mapping[tuple[str, str], GeometryInfo],
    replay_touched_cells: set[Cell] | None,
    ground_y: int,
    max_route_step_cheb: int,
    jump_cone: JumpConeConfig | None,
) -> tuple[CorridorValidationScore, ...]:
    """Validate each corridor's path as if it were the whole route,
    then attribute findings by Chebyshev proximity to the path.

    The per-corridor pass runs the SAME validators against a single
    corridor's path_cells; findings come back scoped automatically
    (route_gap for this corridor, jumps at this corridor's takeoffs,
    etc.). The one finding class that needs proximity filtering is
    partial_multicell — that runs on the full map regardless of
    route, so we filter to cells near this corridor's path only.
    """
    out: list[CorridorValidationScore] = []
    for corridor_id, interval_index, path_iter in corridor_paths:
        path_cells_list = [tuple(c) for c in path_iter]
        if not path_cells_list:
            continue
        path_cells_set = set(path_cells_list)

        geom_rpt = validate_map_geometry(
            blocks=normalised_blocks,
            geometry_lookup=geometry_lookup,
            route_cells=path_cells_list,
            ground_y=ground_y,
            max_route_step_cheb=max_route_step_cheb,
        )
        # partial_multicell runs globally; keep only hits "near" the
        # corridor so two corridors on opposite ends of a map don't
        # both get blamed for the same stripper drop-out.
        partial_hits = sum(
            1
            for f in geom_rpt.by_code(CODE_PARTIAL_MULTICELL)
            if f.severity == SEVERITY_FAIL and _near(path_cells_set, f.cell)
        )
        missing_hits = len(geom_rpt.by_code(CODE_MISSING_SUPPORT))
        route_gap_hits = len(geom_rpt.by_code(CODE_ROUTE_GAP))

        jumps = 0, 0, 0, 0  # broken, uncertain, plausible, replay
        if len(path_cells_list) >= 2:
            from src.generation.jump_validator import (
                CLASS_GEOMETRICALLY_PLAUSIBLE,
                CLASS_LIKELY_BROKEN,
                CLASS_SUPPORTED_BY_REPLAY,
                CLASS_UNCERTAIN,
                validate_jumps,
            )
            jrpt = validate_jumps(
                blocks=normalised_blocks,
                geometry_lookup=geometry_lookup,
                route_cells=path_cells_list,
                replay_touched_cells=replay_touched_cells,
                cone=jump_cone,
            )
            jumps = (
                len(jrpt.by_class(CLASS_LIKELY_BROKEN)),
                len(jrpt.by_class(CLASS_UNCERTAIN)),
                len(jrpt.by_class(CLASS_GEOMETRICALLY_PLAUSIBLE)),
                len(jrpt.by_class(CLASS_SUPPORTED_BY_REPLAY)),
            )
        broken, uncertain, plausible, replay = jumps

        score = _corridor_validation_score(
            partial_multicell_hits=partial_hits,
            missing_support_hits=missing_hits,
            route_gap_hits=route_gap_hits,
            jump_likely_broken=broken,
            jump_uncertain=uncertain,
        )
        out.append(CorridorValidationScore(
            corridor_id=int(corridor_id),
            interval_index=int(interval_index),
            path_length=len(path_cells_list),
            partial_multicell_hits=partial_hits,
            missing_support_hits=missing_hits,
            route_gap_hits=route_gap_hits,
            jump_likely_broken=broken,
            jump_uncertain=uncertain,
            jump_geometrically_plausible=plausible,
            jump_supported_by_replay=replay,
            validation_score=score,
        ))
    return tuple(out)


def _compact_counts(counts: Mapping[str, int]) -> str:
    """Formatter for one-line log summaries — keeps structured logs
    greppable without pulling in a serializer for a 2-line string."""
    return ",".join(
        f"{k}={v}" for k, v in sorted(counts.items()) if v > 0
    ) or "-"


# Re-export the codes consumers care about so they don't have to
# import from two places when reading the summary.
RELEVANT_CODES = frozenset({
    CODE_PARTIAL_MULTICELL,
    CODE_ROUTE_GAP,
    CODE_ROUTE_CELL_MISSING_BLOCK,
    CODE_MISSING_SUPPORT,
    CODE_SPAWN_INTERSECT,
})
