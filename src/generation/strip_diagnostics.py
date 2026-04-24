"""Strip-failure diagnostic (#217 follow-up).

Compares a base map's parsed GBX against the stripped GBX emitted by
``generate-map --strip`` + ``emit-gbx``, and produces a structured
report naming every block the strip removed from the physical
neighbourhood of the chosen route. The aim is evidence, not fixes:
operator in-game testing reported drivability failures on map 1212
that survived #217's ``halo_axis_1_plus_anchor_radius_3_vext_3``
policy; this module's job is to show exactly which blocks went
missing so a principled fix (bigger radius, type-based preservation,
or GBX mesh introspection) can be chosen from data.

Public entry point: :func:`diagnose_strip`.

Design:
- Pure functions over already-parsed GBX dicts (what the wrapper's
  ``map`` command emits). The CLI wraps this with subprocess calls.
- No DB reads; the chosen-corridor set is either passed in by the
  caller (from the generator's artifact JSON) or omitted for a
  geometry-only view.
- Name-pattern heuristics ONLY for multi-cell candidates — we
  deliberately don't guess mesh dimensions. The report flags
  suspect names and leaves interpretation to the operator.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

_LOG = logging.getLogger(__name__)

Cell = tuple[int, int, int]

# Name patterns that usually indicate a block occupies more than one
# grid cell (slopes split across levels, long variants, loops, etc.).
# Used to flag candidates, not to declare dimensions. An operator
# with the emitted report eyeballs the list and decides whether mesh
# introspection is worth the work.
_MULTICELL_NAME_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    r"Slope\d+",                  # Slope2Up, Slope3Down — N-cell slope
    r"Slope2|Slope3|Slope4",      # Explicit multi-cell slopes
    r"Long",                      # RoadTechLong etc.
    r"Loop(?![A-Z])",             # Loop (but not LoopIn etc.)
    r"Loop\d+",                   # Loop1, Loop2 as full-loop variants
    r"Big",                       # *Big* variants
    r"Large",
    r"\d{2,3}m(?![A-Za-z])",      # 16m, 24m, 32m, 64m size suffixes
    r"Curve[23]",                 # Curve2, Curve3 — multi-cell curves
    r"Expandable",                # GateExpandableFinish — the map-2839 case
    r"UHalf",                     # PlatformUHalfHeight...
    r"TwoWay",                    # Multi-direction connectors often multi-cell
))

# Shape-class patterns used to partition dropped blocks for the report.
# Keeps the "what kind of geometry went missing" summary readable.
_RAMP_KEYWORDS = ("slope", "ramp", "bump")
_LOOP_KEYWORDS = ("loop",)
_CURVE_KEYWORDS = ("curve", "bend", "turn")
_SUPPORT_KEYWORDS = ("pillar", "structure", "base", "support", "deadend")
_ANCHOR_KEYWORDS = (
    "start", "spawn", "checkpoint", "finish", "goal", "multilap",
    "linkedcheckpoint",
)


# ---------------------------------------------------------------------
# Public report shape
# ---------------------------------------------------------------------

@dataclass
class DroppedBlock:
    name: str
    family: str
    cell: Cell
    placement: str           # "grid" | "free"
    distance_to_route_cheb: int | None  # None when no route cells supplied
    nearest_route_cell: Cell | None = None
    is_multicell_candidate: bool = False
    near_anchor: str | None = None       # "Spawn" / "Checkpoint#1" / None


@dataclass
class AnchorSurroundDiff:
    tag: str
    order: int
    anchor_cell: Cell | None             # grid anchor cell or snapped
    radius: int
    base_blocks: int
    kept_blocks: int
    dropped_blocks: list[DroppedBlock] = field(default_factory=list)


@dataclass
class RouteCellDiff:
    cell: Cell
    dropped_within_radius: list[DroppedBlock] = field(default_factory=list)


@dataclass
class DiagnosticReport:
    base_map_id: int
    base_block_count: int
    stripped_block_count: int
    base_free_count: int
    stripped_free_count: int
    baked_block_count: int          # environment scenery — unchanged
    dropped_by_shape_bucket: dict[str, int] = field(default_factory=dict)
    dropped_by_family: dict[str, int] = field(default_factory=dict)
    multicell_candidate_drops: list[DroppedBlock] = field(default_factory=list)
    anchor_surrounds: list[AnchorSurroundDiff] = field(default_factory=list)
    route_cell_drops: list[RouteCellDiff] = field(default_factory=list)
    total_route_cells: int = 0
    # Hypotheses — populated by the summariser; each is a short
    # sentence derived from the numbers above.
    hypotheses: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _block_cell_grid(block: dict[str, Any]) -> Cell | None:
    if block.get("placement") != "grid":
        return None
    x, y, z = block.get("x"), block.get("y"), block.get("z")
    if x is None or y is None or z is None:
        return None
    return (int(x), int(y), int(z))


def _block_signature(block: dict[str, Any]) -> tuple:
    """Stable identity for a block. Grid blocks keyed on cell +
    name; free blocks keyed on abs coords (floated to int cm) +
    name. Used to compare base vs stripped block sets."""
    name = str(block.get("name", ""))
    if block.get("placement") == "grid":
        return ("grid", name,
                int(block.get("x") or 0),
                int(block.get("y") or 0),
                int(block.get("z") or 0),
                int(block.get("direction_index", 0))
                if isinstance(block.get("direction_index"), int)
                else 0,
                int(block.get("variant") or 0),
                int(block.get("sub_variant") or 0))
    return ("free", name,
            int(round(float(block.get("abs_x") or 0.0) * 100)),
            int(round(float(block.get("abs_y") or 0.0) * 100)),
            int(round(float(block.get("abs_z") or 0.0) * 100)))


def _chebyshev(a: Cell, b: Cell) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def _is_multicell_candidate(name: str) -> bool:
    return any(p.search(name) for p in _MULTICELL_NAME_PATTERNS)


def _shape_bucket(name: str) -> str:
    lname = name.lower()
    if any(k in lname for k in _ANCHOR_KEYWORDS):
        return "anchor"
    if any(k in lname for k in _LOOP_KEYWORDS):
        return "loop"
    if any(k in lname for k in _RAMP_KEYWORDS):
        return "ramp"
    if any(k in lname for k in _CURVE_KEYWORDS):
        return "curve"
    if any(k in lname for k in _SUPPORT_KEYWORDS):
        return "support"
    return "other"


# ---------------------------------------------------------------------
# Core diffing
# ---------------------------------------------------------------------

def _split_dropped_kept(
    base_blocks: list[dict[str, Any]],
    stripped_blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (dropped_blocks, kept_blocks) as viewed from the base
    side. A block is "dropped" if its signature doesn't appear in
    the stripped set; "kept" if it does."""
    stripped_sigs = {_block_signature(b) for b in stripped_blocks}
    dropped: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for b in base_blocks:
        if _block_signature(b) in stripped_sigs:
            kept.append(b)
        else:
            dropped.append(b)
    return dropped, kept


def _route_cell_set(chosen_corridor_cells: Iterable[Cell] | None) -> set[Cell]:
    if chosen_corridor_cells is None:
        return set()
    return {(int(c[0]), int(c[1]), int(c[2])) for c in chosen_corridor_cells}


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def diagnose_strip(
    *,
    base_map_id: int,
    base_map: dict[str, Any],
    stripped_map: dict[str, Any],
    chosen_corridor_cells: Iterable[Cell] | None = None,
    anchor_cells: list[tuple[str, int, Cell | None]] | None = None,
    route_cell_radius: int = 2,
    anchor_surround_radius: int = 3,
) -> DiagnosticReport:
    """Build a :class:`DiagnosticReport` from parsed GBX dicts.

    Parameters
    ----------
    base_map, stripped_map : the "output" dicts from the wrapper's
        ``map`` command on each file.
    chosen_corridor_cells : flattened list of cells on the generator's
        chosen route (e.g. all corridors' ``path_cells`` concatenated);
        used to compute per-route-cell drop neighbourhoods.
    anchor_cells : list of (tag, order, cell) tuples — grid anchor
        cell or snapped-from-free cell. ``None`` cell means the
        anchor is free-placed and no surround analysis is run.
    route_cell_radius, anchor_surround_radius : Chebyshev radii used
        when counting "nearby" drops.
    """
    base_blocks = list(base_map.get("blocks") or [])
    stripped_blocks = list(stripped_map.get("blocks") or [])
    dropped, _kept = _split_dropped_kept(base_blocks, stripped_blocks)

    base_grid = [b for b in base_blocks if b.get("placement") == "grid"]
    base_free = [b for b in base_blocks if b.get("placement") == "free"]
    stripped_grid = [
        b for b in stripped_blocks if b.get("placement") == "grid"
    ]
    stripped_free = [
        b for b in stripped_blocks if b.get("placement") == "free"
    ]

    report = DiagnosticReport(
        base_map_id=base_map_id,
        base_block_count=len(base_grid),
        stripped_block_count=len(stripped_grid),
        base_free_count=len(base_free),
        stripped_free_count=len(stripped_free),
        baked_block_count=int(base_map.get("baked_block_count") or 0),
        total_route_cells=(
            len(list(chosen_corridor_cells))
            if chosen_corridor_cells is not None else 0
        ),
    )

    route_set = _route_cell_set(chosen_corridor_cells)
    # We may have consumed the iterator to count — rebuild from the
    # set so later callers still see the data.
    chosen_cells = sorted(route_set) if route_set else []

    anchor_lookup: dict[Cell, tuple[str, int]] = {}
    if anchor_cells:
        for tag, order, cell in anchor_cells:
            if cell is not None:
                anchor_lookup[cell] = (tag, int(order))

    # Build DroppedBlock objects. Compute distance to nearest route
    # cell once per dropped block; O(drops × route_cells) — cheap on
    # real sizes (few hundred × few dozen).
    dropped_blocks: list[DroppedBlock] = []
    for b in dropped:
        cell = _block_cell_grid(b)
        if cell is None:
            # Free block that got dropped (shouldn't happen with the
            # current MapEmitter filter, but carry it through for
            # completeness).
            dropped_blocks.append(DroppedBlock(
                name=str(b.get("name", "")),
                family="(free-placed)",
                cell=(0, 0, 0),
                placement="free",
                distance_to_route_cheb=None,
            ))
            continue
        nearest_route: Cell | None = None
        nearest_dist: int | None = None
        for rc in chosen_cells:
            d = _chebyshev(cell, rc)
            if nearest_dist is None or d < nearest_dist:
                nearest_dist = d
                nearest_route = rc
        # Proximity to any anchor cell.
        near_anchor_label: str | None = None
        for a_cell, (tag, order) in anchor_lookup.items():
            if _chebyshev(cell, a_cell) <= anchor_surround_radius:
                near_anchor_label = f"{tag}#{order}"
                break
        dropped_blocks.append(DroppedBlock(
            name=str(b.get("name", "")),
            family=_family_from_name(str(b.get("name", ""))),
            cell=cell,
            placement="grid",
            distance_to_route_cheb=nearest_dist,
            nearest_route_cell=nearest_route,
            is_multicell_candidate=_is_multicell_candidate(
                str(b.get("name", ""))
            ),
            near_anchor=near_anchor_label,
        ))

    # Shape + family breakdowns.
    for db in dropped_blocks:
        bucket = _shape_bucket(db.name)
        report.dropped_by_shape_bucket[bucket] = (
            report.dropped_by_shape_bucket.get(bucket, 0) + 1
        )
        report.dropped_by_family[db.family] = (
            report.dropped_by_family.get(db.family, 0) + 1
        )

    report.multicell_candidate_drops = [
        db for db in dropped_blocks if db.is_multicell_candidate
    ]

    # Per-anchor surround.
    for a_cell, (tag, order) in anchor_lookup.items():
        base_in_radius = 0
        kept_in_radius = 0
        dropped_in_radius: list[DroppedBlock] = []
        for b in base_grid:
            cell = _block_cell_grid(b)
            if cell is None:
                continue
            if _chebyshev(cell, a_cell) <= anchor_surround_radius:
                base_in_radius += 1
        for b in stripped_grid:
            cell = _block_cell_grid(b)
            if cell is None:
                continue
            if _chebyshev(cell, a_cell) <= anchor_surround_radius:
                kept_in_radius += 1
        for db in dropped_blocks:
            if db.placement != "grid":
                continue
            if _chebyshev(db.cell, a_cell) <= anchor_surround_radius:
                dropped_in_radius.append(db)
        report.anchor_surrounds.append(AnchorSurroundDiff(
            tag=tag, order=order, anchor_cell=a_cell,
            radius=anchor_surround_radius,
            base_blocks=base_in_radius,
            kept_blocks=kept_in_radius,
            dropped_blocks=dropped_in_radius,
        ))

    # Per-route-cell drop neighbourhood.
    for rc in chosen_cells:
        drops_here: list[DroppedBlock] = []
        for db in dropped_blocks:
            if db.placement != "grid":
                continue
            if _chebyshev(db.cell, rc) <= route_cell_radius:
                drops_here.append(db)
        if drops_here:
            report.route_cell_drops.append(RouteCellDiff(
                cell=rc, dropped_within_radius=drops_here,
            ))

    _populate_hypotheses(report)

    return report


def _family_from_name(name: str) -> str:
    """Best-effort block family from the block_name. The wrapper's
    ``name`` field is the full block ID (e.g. ``PlatformPlasticStart``);
    we take the first CamelCase word as the family ("Platform").
    Imperfect on compound prefixes (RoadTech, RoadDirt etc. fold to
    "Road") but the family breakdown is a grouping for readability,
    not a taxonomic claim."""
    m = re.match(r"[A-Z][a-z]+", name or "")
    return m.group(0) if m else ""


# ---------------------------------------------------------------------
# Hypothesis generation
# ---------------------------------------------------------------------

def _populate_hypotheses(report: DiagnosticReport) -> None:
    """Derive short, operator-readable sentences from the counts."""
    total_dropped = report.base_block_count - report.stripped_block_count
    if total_dropped <= 0:
        report.hypotheses.append(
            "No net drops — strip preserved every grid block. "
            "Failure is NOT a strip-drops issue."
        )
        return

    multicell = len(report.multicell_candidate_drops)
    if multicell:
        report.hypotheses.append(
            f"{multicell} dropped block(s) carry names suggesting "
            "multi-cell geometry (slopes, loops, size-suffixed, etc.). "
            "If those blocks' origins were KEPT but their extent cells "
            "were dropped, the in-game map will render them partially "
            "— matching the 'spherical shapes only half shown' symptom. "
            "The inverse (origin dropped but extent cell KEPT) leaves "
            "visible half-geometry at the wrong position."
        )

    ramp_drops = report.dropped_by_shape_bucket.get("ramp", 0)
    support_drops = report.dropped_by_shape_bucket.get("support", 0)
    if ramp_drops and report.route_cell_drops:
        # Are ramps dropping NEXT TO route cells?
        near_ramps = sum(
            1 for rcd in report.route_cell_drops
            for db in rcd.dropped_within_radius
            if _shape_bucket(db.name) == "ramp"
        )
        if near_ramps:
            report.hypotheses.append(
                f"{near_ramps} ramp-class block(s) dropped within "
                "route-cell radius 2 — this is the canonical "
                "'drive off start, end up under next block' pattern: "
                "the ramp connecting two tiers got stripped."
            )

    if support_drops:
        report.hypotheses.append(
            f"{support_drops} support-class block(s) (pillar / base / "
            "structure / deadend) dropped. With the surface removed "
            "from below, drivable cells read as 'floating road' and "
            "cars that fall short of a jump land inside the baked "
            "stadium scenery."
        )

    # Anchor-surround check. The user specifically complained about
    # 'from start to first block' — look at Spawn's surround
    # specifically.
    for ads in report.anchor_surrounds:
        if ads.tag.lower() == "spawn":
            loss_pct = (
                (ads.base_blocks - ads.kept_blocks)
                / max(1, ads.base_blocks)
            )
            if loss_pct > 0.3 and (ads.base_blocks - ads.kept_blocks) >= 4:
                report.hypotheses.append(
                    f"Spawn surround lost {ads.base_blocks - ads.kept_blocks} "
                    f"of {ads.base_blocks} blocks within Chebyshev "
                    f"{ads.radius} ({loss_pct:.0%}). The start ramp "
                    "cluster isn't being preserved densely enough."
                )


# ---------------------------------------------------------------------
# Markdown emission
# ---------------------------------------------------------------------

def format_report_markdown(
    report: DiagnosticReport,
    *,
    run_id: str | None = None,
    strip_policy: str | None = None,
) -> str:
    """Render a :class:`DiagnosticReport` as a markdown document
    suitable for ``reports/strip-diagnostics/``."""
    lines: list[str] = []
    lines.append(f"# Strip-failure diagnostic — map {report.base_map_id}")
    if run_id:
        lines.append(f"\n**run_id:** `{run_id}`")
    if strip_policy:
        lines.append(f"**strip_policy:** `{strip_policy}`")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    lines.append("| metric | base | stripped | delta |")
    lines.append("|---|---:|---:|---:|")
    grid_delta = report.stripped_block_count - report.base_block_count
    free_delta = report.stripped_free_count - report.base_free_count
    lines.append(
        f"| grid blocks | {report.base_block_count} | "
        f"{report.stripped_block_count} | {grid_delta:+d} |"
    )
    lines.append(
        f"| free blocks | {report.base_free_count} | "
        f"{report.stripped_free_count} | {free_delta:+d} |"
    )
    lines.append(
        f"| baked blocks | {report.baked_block_count} | "
        f"{report.baked_block_count} | 0 |"
    )
    lines.append("")

    # Hypotheses
    lines.append("## Likely reasons")
    lines.append("")
    if report.hypotheses:
        for h in report.hypotheses:
            lines.append(f"- {h}")
    else:
        lines.append("*No hypotheses generated.*")
    lines.append("")

    # Shape breakdown
    lines.append("## Dropped blocks — shape bucket breakdown")
    lines.append("")
    if report.dropped_by_shape_bucket:
        total = sum(report.dropped_by_shape_bucket.values())
        lines.append("| bucket | count | %of dropped |")
        lines.append("|---|---:|---:|")
        for bucket, count in sorted(
            report.dropped_by_shape_bucket.items(),
            key=lambda p: -p[1],
        ):
            pct = (count / total) * 100 if total else 0
            lines.append(f"| {bucket} | {count} | {pct:.0f}% |")
    else:
        lines.append("*No drops.*")
    lines.append("")

    # Family breakdown
    lines.append("## Dropped blocks — by family (top 15)")
    lines.append("")
    if report.dropped_by_family:
        lines.append("| family | count |")
        lines.append("|---|---:|")
        top = sorted(
            report.dropped_by_family.items(), key=lambda p: -p[1]
        )[:15]
        for fam, count in top:
            lines.append(f"| {fam or '(empty)'} | {count} |")
    lines.append("")

    # Anchor surrounds
    lines.append("## Anchor surround preservation (radius 3)")
    lines.append("")
    if report.anchor_surrounds:
        lines.append("| anchor | cell | base | kept | dropped |")
        lines.append("|---|---|---:|---:|---:|")
        for ads in report.anchor_surrounds:
            c = ads.anchor_cell
            lines.append(
                f"| {ads.tag}#{ads.order} | "
                f"({c[0]}, {c[1]}, {c[2]}) | "
                f"{ads.base_blocks} | {ads.kept_blocks} | "
                f"{ads.base_blocks - ads.kept_blocks} |"
            )
        lines.append("")
        # Detail per anchor with dropped blocks
        for ads in report.anchor_surrounds:
            if not ads.dropped_blocks:
                continue
            lines.append(
                f"### {ads.tag}#{ads.order} — dropped near anchor"
            )
            lines.append("")
            lines.append("| cell | block name | multicell? |")
            lines.append("|---|---|:---:|")
            for db in ads.dropped_blocks[:30]:
                flag = "✓" if db.is_multicell_candidate else ""
                lines.append(
                    f"| ({db.cell[0]}, {db.cell[1]}, {db.cell[2]}) "
                    f"| `{db.name}` | {flag} |"
                )
            if len(ads.dropped_blocks) > 30:
                lines.append(
                    f"| … | *({len(ads.dropped_blocks) - 30} more)* | |"
                )
            lines.append("")
    else:
        lines.append("*No anchor cells provided to analyser.*")
        lines.append("")

    # Multi-cell candidates
    lines.append("## Multi-cell candidate drops")
    lines.append("")
    if report.multicell_candidate_drops:
        # Dedupe by name for readability; show counts.
        by_name: dict[str, int] = {}
        nearest_examples: dict[str, DroppedBlock] = {}
        for db in report.multicell_candidate_drops:
            by_name[db.name] = by_name.get(db.name, 0) + 1
            if db.name not in nearest_examples or (
                (db.distance_to_route_cheb or 99)
                < (nearest_examples[db.name].distance_to_route_cheb or 99)
            ):
                nearest_examples[db.name] = db
        lines.append(
            "| block name | count dropped | nearest route-cell distance |"
        )
        lines.append("|---|---:|---:|")
        for name, count in sorted(by_name.items(), key=lambda p: -p[1]):
            ex = nearest_examples[name]
            d = ex.distance_to_route_cheb
            d_str = "—" if d is None else str(d)
            lines.append(f"| `{name}` | {count} | {d_str} |")
    else:
        lines.append(
            "*No blocks with multi-cell-suggesting names were dropped.*"
        )
    lines.append("")

    # Route-cell drop neighbourhoods
    lines.append("## Drops within route-cell radius 2")
    lines.append("")
    if report.route_cell_drops:
        lines.append(
            f"{len(report.route_cell_drops)} route cell(s) have dropped "
            "blocks within Chebyshev distance 2. These are the stretches "
            "of the chosen route where the strip policy's anchor cubes "
            "and vertical extension didn't overlap — the concrete "
            "candidates for 'drive-off-the-edge' and 'lands-in-block' "
            "symptoms."
        )
        lines.append("")
        for rcd in report.route_cell_drops:
            lines.append(
                f"### Route cell `{rcd.cell}` — "
                f"{len(rcd.dropped_within_radius)} dropped within radius 2"
            )
            lines.append("")
            lines.append("| cheb | cell | block name | multicell? |")
            lines.append("|---:|---|---|:---:|")
            # Sort nearest-first so the most likely culprits lead.
            sorted_drops = sorted(
                rcd.dropped_within_radius,
                key=lambda db: (
                    db.distance_to_route_cheb
                    if db.distance_to_route_cheb is not None else 99
                ),
            )
            for db in sorted_drops:
                d = db.distance_to_route_cheb
                d_str = "—" if d is None else str(d)
                flag = "✓" if db.is_multicell_candidate else ""
                lines.append(
                    f"| {d_str} | "
                    f"({db.cell[0]}, {db.cell[1]}, {db.cell[2]}) | "
                    f"`{db.name}` | {flag} |"
                )
            lines.append("")
    else:
        lines.append(
            "*No blocks dropped within route-cell radius 2.*"
        )
    lines.append("")

    # What we don't know
    lines.append("## What this report can't tell you")
    lines.append("")
    lines.append(
        "- **Actual block dimensions.** We store one origin cell per "
        "block placement; the block's mesh may span more cells but we "
        "don't have that from our parse today. GBX block-catalog "
        "introspection (via ``CGameCtnBlockInfo``) is the next step "
        "if multi-cell candidates turn out to be the failure mode."
    )
    lines.append(
        "- **Rotation-dependent geometry.** The same block at rotation "
        "0 vs rotation 1 may occupy different cells; name patterns "
        "can't reveal that."
    )
    lines.append(
        "- **Visual / collision-mesh edges.** A block's renderable "
        "geometry may extend into non-occupied cells (overhang, "
        "ramp projections, etc.); those won't appear in any position "
        "data we have."
    )
    lines.append("")

    return "\n".join(lines) + "\n"
