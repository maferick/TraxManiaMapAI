"""Geometry validity checker v0 (task #226).

A Layer-1 structural validator for generated / stripped maps — NOT a
physics simulation and NOT a replacement for the finishability gate.
Its job is to catch obviously-broken geometry before we bake a GBX:

- multi-cell blocks whose origin survived strip but whose "shadow"
  cells got removed (the headline failure from map 1212 diagnostics)
- route-continuity gaps exceeding a configurable car-step ceiling
- missing support beneath elevated route cells
- spawn cells occupied by non-start geometry

Scope boundary (CLAUDE.md §Replay-ground-truth learning contract):
this module produces findings only. It never rejects a map, never
promotes a transition, and never talks to the traversability graph
or the replay store. Generator-scoring / gate code consumes the
report; the validator itself stays hermetic.

Usage
-----

    report = validate_map_geometry(
        blocks=parsed_blocks,
        geometry_lookup=catalog_dict,
        route_cells=chosen_route_cells,
        spawn_cell=spawn_cell_or_none,
    )
    for f in report.findings:
        ...
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

_LOG = logging.getLogger(__name__)

Cell = tuple[int, int, int]

# ---------------------------------------------------------------------
# Severity / finding codes
# ---------------------------------------------------------------------

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_FAIL = "fail"

# Codes are a closed vocabulary — downstream filtering depends on
# stable strings. Adding one is a PR; renaming one is a breaking
# change for consumers.
CODE_PARTIAL_MULTICELL = "partial_multicell"
CODE_ROUTE_GAP = "route_gap"
CODE_ROUTE_CELL_MISSING_BLOCK = "route_cell_missing_block"
CODE_MISSING_SUPPORT = "missing_support"
CODE_SPAWN_INTERSECT = "spawn_intersect"
CODE_UNKNOWN_BLOCK = "unknown_block"


# ---------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class BlockRef:
    """Thin identity object for referencing a block in a Finding."""
    family: str
    name: str
    cell: Cell

    def __str__(self) -> str:
        return f"{self.family}/{self.name}@{self.cell}"


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    detail: str
    cell: Cell | None = None
    block: BlockRef | None = None


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)
    blocks_total: int = 0
    grid_blocks_total: int = 0
    route_cells_total: int = 0

    @property
    def has_failures(self) -> bool:
        return any(f.severity == SEVERITY_FAIL for f in self.findings)

    @property
    def has_warnings(self) -> bool:
        return any(f.severity == SEVERITY_WARN for f in self.findings)

    def by_code(self, code: str) -> list[Finding]:
        return [f for f in self.findings if f.code == code]


@dataclass(frozen=True)
class GeometryInfo:
    """Subset of block_geometry columns the validator depends on.

    Kept separate from :class:`src.constraints.block_geometry.BlockGeometry`
    so this module doesn't import from constraints (keeping the layer
    boundary clean — constraints is DB-aware, validator is pure).
    """
    footprint_x: int = 1
    footprint_y: int = 1
    footprint_z: int = 1
    connector_hint: str = ""
    shape_class: str = "unknown"
    is_anchor_capable: bool = False


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _block_cell(block: Mapping[str, Any]) -> Cell | None:
    if block.get("placement") != "grid":
        return None
    try:
        return (int(block["x"]), int(block["y"]), int(block["z"]))
    except (KeyError, TypeError, ValueError):
        return None


def _block_ref(block: Mapping[str, Any], cell: Cell) -> BlockRef:
    return BlockRef(
        family=str(block.get("family") or ""),
        name=str(block.get("name") or ""),
        cell=cell,
    )


def _chebyshev(a: Cell, b: Cell) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def _lookup_key(family: str, name: str) -> tuple[str, str]:
    # Match the upsert key in block_geometry — (family, name) pair.
    return (family or "", name or "")


def _footprint_shadow_cells(
    origin: Cell, rotation: int, footprint_x: int,
) -> list[Cell]:
    """Cells occupied by a block with footprint_x>1 at the given rotation.

    v0 scope: only X-length footprints. Multi-axis footprints need
    mesh-level data (M1/M2 workstream). Returns ``[origin]`` for
    unit-footprint blocks.

    Rotation is 0..3 quarter-turns in the XZ plane (the Trackmania
    convention). At rotation 0 the extension runs along +X; each
    quarter turn rotates the direction clockwise when viewed from
    above.
    """
    x, y, z = origin
    if footprint_x <= 1:
        return [origin]
    rot = rotation & 0b11
    if rot == 0:
        return [(x + i, y, z) for i in range(footprint_x)]
    if rot == 1:
        return [(x, y, z + i) for i in range(footprint_x)]
    if rot == 2:
        return [(x - i, y, z) for i in range(footprint_x)]
    return [(x, y, z - i) for i in range(footprint_x)]  # rot == 3


# ---------------------------------------------------------------------
# Individual checks — each returns a list of Finding objects.
#
# Checks are pure functions over an already-indexed map state
# (``cell_to_block`` dict) so the orchestrator does the indexing once.
# ---------------------------------------------------------------------

def check_partial_multicell(
    *,
    grid_blocks: list[Mapping[str, Any]],
    cell_to_block: dict[Cell, Mapping[str, Any]],
    geometry_lookup: Mapping[tuple[str, str], GeometryInfo],
) -> list[Finding]:
    """Flag multi-cell blocks whose shadow cells aren't accounted for.

    A block at cell C with ``footprint_x=4`` is supposed to occupy
    cells C, C+1, C+2, C+3 (rotation-dependent). If any of those
    shadow cells is empty *and* not the origin of any other block,
    the map has a partial footprint: the mesh renders but neighbours
    aren't there, so driving ends up under / inside an empty cell
    (the map-1212 symptom).

    Empty shadow cells are the fail signal. Overlap (another block's
    origin inside our shadow) is a *warn* — legitimate in some
    variants but suspicious.
    """
    findings: list[Finding] = []
    for block in grid_blocks:
        cell = _block_cell(block)
        if cell is None:
            continue
        name = str(block.get("name") or "")
        family = str(block.get("family") or "")
        info = geometry_lookup.get(_lookup_key(family, name))
        if info is None or info.footprint_x <= 1:
            continue
        rotation = int(block.get("rotation") or 0)
        shadow = _footprint_shadow_cells(cell, rotation, info.footprint_x)
        # The origin is always the block itself; skip it.
        for shadow_cell in shadow[1:]:
            occupant = cell_to_block.get(shadow_cell)
            if occupant is None:
                findings.append(Finding(
                    severity=SEVERITY_FAIL,
                    code=CODE_PARTIAL_MULTICELL,
                    detail=(
                        f"footprint cell {shadow_cell} empty — "
                        f"{family}/{name} (fx={info.footprint_x}, "
                        f"rot={rotation}) expects mesh there"
                    ),
                    cell=shadow_cell,
                    block=_block_ref(block, cell),
                ))
            elif occupant is not block:
                # Another block's origin sits inside our shadow. The
                # two meshes may compose legitimately (deco overlays)
                # or may actually collide; flag but don't fail.
                findings.append(Finding(
                    severity=SEVERITY_WARN,
                    code=CODE_PARTIAL_MULTICELL,
                    detail=(
                        f"footprint cell {shadow_cell} overlapped by "
                        f"{occupant.get('family')}/{occupant.get('name')}; "
                        f"origin at {_block_ref(block, cell)}"
                    ),
                    cell=shadow_cell,
                    block=_block_ref(block, cell),
                ))
    return findings


def check_route_continuity(
    *,
    route_cells: list[Cell],
    max_step_cheb: int,
) -> list[Finding]:
    """Flag consecutive route cells farther apart than ``max_step_cheb``.

    Route cells come in traversal order. A gap larger than one cell
    in Chebyshev distance is a candidate for "car can't cross" —
    unless a jump is involved, which the jump-aware validator (task
    #227) handles. v0 reports gaps as warnings so generation-scoring
    can weight them without hard-rejecting routes that cross a
    legitimate ramp launch.
    """
    findings: list[Finding] = []
    if len(route_cells) < 2:
        return findings
    for i in range(1, len(route_cells)):
        a, b = route_cells[i - 1], route_cells[i]
        d = _chebyshev(a, b)
        if d > max_step_cheb:
            findings.append(Finding(
                severity=SEVERITY_WARN,
                code=CODE_ROUTE_GAP,
                detail=(
                    f"route step cheb={d} between {a} and {b} "
                    f"(max={max_step_cheb}); jump or strip dropout?"
                ),
                cell=b,
            ))
    return findings


def check_route_cells_have_blocks(
    *,
    route_cells: list[Cell],
    cell_to_block: dict[Cell, Mapping[str, Any]],
) -> list[Finding]:
    """Fail when a route cell has no block at all.

    The car has to be standing on *something*. This is the absolute-
    minimum floor: even before we worry about shape matching or
    connector compatibility, an empty cell in the route cannot be
    driven through.
    """
    findings: list[Finding] = []
    for cell in route_cells:
        if cell not in cell_to_block:
            findings.append(Finding(
                severity=SEVERITY_FAIL,
                code=CODE_ROUTE_CELL_MISSING_BLOCK,
                detail=f"route cell {cell} has no block",
                cell=cell,
            ))
    return findings


_SELF_SUPPORTING_SHAPES = frozenset({
    "ramp", "loop", "platform", "start", "checkpoint", "finish",
})


def check_missing_support(
    *,
    route_cells: list[Cell],
    cell_to_block: dict[Cell, Mapping[str, Any]],
    geometry_lookup: Mapping[tuple[str, str], GeometryInfo],
    ground_y: int,
) -> list[Finding]:
    """Flag elevated route cells with nothing beneath them.

    Trackmania's drivable surface either rests on the ground
    (y == ground_y for Stadium) or on another block at y-1. Shapes
    like ramps, platforms, and anchor blocks are self-supporting by
    mesh (they carry their own undercarriage) so a missing block
    below them is not a finding.
    """
    findings: list[Finding] = []
    for cell in route_cells:
        x, y, z = cell
        if y <= ground_y:
            continue
        block = cell_to_block.get(cell)
        if block is None:
            continue  # route_cells_have_blocks already flagged this
        info = geometry_lookup.get(_lookup_key(
            str(block.get("family") or ""),
            str(block.get("name") or ""),
        ))
        if info is not None and info.shape_class in _SELF_SUPPORTING_SHAPES:
            continue
        below = cell_to_block.get((x, y - 1, z))
        if below is None:
            findings.append(Finding(
                severity=SEVERITY_WARN,
                code=CODE_MISSING_SUPPORT,
                detail=(
                    f"route cell {cell} elevated (y>{ground_y}) with no "
                    f"block at {(x, y - 1, z)}"
                ),
                cell=cell,
                block=_block_ref(block, cell),
            ))
    return findings


def check_spawn_intersect(
    *,
    spawn_cell: Cell | None,
    cell_to_block: dict[Cell, Mapping[str, Any]],
    geometry_lookup: Mapping[tuple[str, str], GeometryInfo],
) -> list[Finding]:
    """Fail when a non-start block occupies the spawn cell or its +Y.

    The spawn cell must carry a Start-capable block; the cell above
    it must be clear so the car has headroom. A non-start block in
    either slot produces the map-1212 "car inside geometry" symptom.
    """
    findings: list[Finding] = []
    if spawn_cell is None:
        return findings
    x, y, z = spawn_cell
    occupant = cell_to_block.get(spawn_cell)
    if occupant is not None:
        info = geometry_lookup.get(_lookup_key(
            str(occupant.get("family") or ""),
            str(occupant.get("name") or ""),
        ))
        if info is None or not info.is_anchor_capable:
            findings.append(Finding(
                severity=SEVERITY_FAIL,
                code=CODE_SPAWN_INTERSECT,
                detail=(
                    f"spawn cell {spawn_cell} occupied by non-anchor "
                    f"{occupant.get('family')}/{occupant.get('name')}"
                ),
                cell=spawn_cell,
                block=_block_ref(occupant, spawn_cell),
            ))
    headroom = (x, y + 1, z)
    above = cell_to_block.get(headroom)
    if above is not None:
        findings.append(Finding(
            severity=SEVERITY_FAIL,
            code=CODE_SPAWN_INTERSECT,
            detail=(
                f"spawn headroom cell {headroom} occupied by "
                f"{above.get('family')}/{above.get('name')}"
            ),
            cell=headroom,
            block=_block_ref(above, headroom),
        ))
    return findings


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------

def validate_map_geometry(
    *,
    blocks: Iterable[Mapping[str, Any]],
    geometry_lookup: Mapping[tuple[str, str], GeometryInfo],
    route_cells: Iterable[Cell] | None = None,
    spawn_cell: Cell | None = None,
    ground_y: int = 9,
    max_route_step_cheb: int = 1,
) -> ValidationReport:
    """Run every v0 check and return a :class:`ValidationReport`.

    Parameters
    ----------
    blocks : iterable of parsed-GBX block dicts (same shape the
        subprocess wrapper emits — ``placement``, ``x/y/z``, ``family``,
        ``name``, ``rotation``).
    geometry_lookup : pre-loaded ``(family, name) -> GeometryInfo``.
        Callers query the ``block_geometry`` table and build this dict
        once per validation run.
    route_cells : traversal-ordered cells of the chosen route. Optional;
        when ``None``, route-specific checks are skipped.
    spawn_cell : the Start block's cell, if known.
    ground_y : y-level below which support-from-below is not required.
        Stadium's floor sits at y=9 in TM2020's grid.
    max_route_step_cheb : largest Chebyshev step between consecutive
        route cells that's still considered continuous (default 1).
    """
    block_list = list(blocks)
    grid_blocks = [b for b in block_list if b.get("placement") == "grid"]
    cell_to_block: dict[Cell, Mapping[str, Any]] = {}
    for b in grid_blocks:
        c = _block_cell(b)
        if c is not None:
            # First writer wins on cell collisions; downstream the
            # partial-multicell check will flag the overlap.
            cell_to_block.setdefault(c, b)

    route = list(route_cells) if route_cells is not None else []

    report = ValidationReport(
        blocks_total=len(block_list),
        grid_blocks_total=len(grid_blocks),
        route_cells_total=len(route),
    )

    # partial_multicell runs regardless of route — it's a
    # self-consistency check on the map's block set.
    report.checks_run.append(CODE_PARTIAL_MULTICELL)
    report.findings.extend(check_partial_multicell(
        grid_blocks=grid_blocks,
        cell_to_block=cell_to_block,
        geometry_lookup=geometry_lookup,
    ))

    if route:
        report.checks_run.append(CODE_ROUTE_GAP)
        report.findings.extend(check_route_continuity(
            route_cells=route, max_step_cheb=max_route_step_cheb,
        ))
        report.checks_run.append(CODE_ROUTE_CELL_MISSING_BLOCK)
        report.findings.extend(check_route_cells_have_blocks(
            route_cells=route, cell_to_block=cell_to_block,
        ))
        report.checks_run.append(CODE_MISSING_SUPPORT)
        report.findings.extend(check_missing_support(
            route_cells=route,
            cell_to_block=cell_to_block,
            geometry_lookup=geometry_lookup,
            ground_y=ground_y,
        ))

    if spawn_cell is not None:
        report.checks_run.append(CODE_SPAWN_INTERSECT)
        report.findings.extend(check_spawn_intersect(
            spawn_cell=spawn_cell,
            cell_to_block=cell_to_block,
            geometry_lookup=geometry_lookup,
        ))

    _LOG.info(
        "validate_map_geometry: blocks=%d grid=%d route_cells=%d "
        "findings=%d (fail=%d warn=%d)",
        report.blocks_total, report.grid_blocks_total,
        report.route_cells_total, len(report.findings),
        sum(1 for f in report.findings if f.severity == SEVERITY_FAIL),
        sum(1 for f in report.findings if f.severity == SEVERITY_WARN),
    )
    return report


def load_geometry_lookup(
    conn: Any, *, families: Iterable[str] | None = None,
) -> dict[tuple[str, str], GeometryInfo]:
    """Read block_geometry into a ``(family, name) -> GeometryInfo`` dict.

    Lives alongside the validator (rather than in constraints) because
    it produces validator-specific DTOs. The read itself is cheap —
    5–6k rows max — so we load the full table into memory and let the
    caller reuse it across many validations.
    """
    from src.storage.mariadb import cursor  # local import keeps module pure
    sql = (
        "SELECT block_family, block_name, shape_class, connector_hint, "
        "       is_anchor_capable, footprint_x, footprint_y, footprint_z "
        "FROM block_geometry"
    )
    params: tuple = ()
    if families:
        fam_list = list(families)
        placeholders = ",".join(["%s"] * len(fam_list))
        sql += f" WHERE block_family IN ({placeholders})"
        params = tuple(fam_list)
    out: dict[tuple[str, str], GeometryInfo] = {}
    with cursor(conn) as cur:
        cur.execute(sql, params)
        for family, name, shape, connector, anchor, fx, fy, fz in cur.fetchall():
            out[(str(family), str(name))] = GeometryInfo(
                footprint_x=int(fx), footprint_y=int(fy), footprint_z=int(fz),
                connector_hint=str(connector or ""),
                shape_class=str(shape or "unknown"),
                is_anchor_capable=bool(anchor),
            )
    return out
