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
) -> PreEmitValidationSummary:
    """Run structural + jump validators and aggregate findings.

    ``blocks`` may arrive in either the DB row shape
    (``block_family`` / ``block_name``) or the parsed-GBX wrapper
    shape (``family`` / ``name``); both are normalised before
    validation so callers don't have to remember which.
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
