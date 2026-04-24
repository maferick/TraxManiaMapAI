"""Phase 2 #218-3 / #218-6 — block-geometry catalogue.

Per-(block_family, block_name) metadata used as a soft input to the
generation-time pattern/geometry compatibility score. Scope per
project-218 doc: soft signal only; frequency + geometry never
override traversability evidence.

The v1 classifier infers shape / surface / role from the block_name
string via pattern matching. This gets 99% of the corpus right on
Nadeo-shipped blocks and is brittle on community custom blocks
(those get shape_class='unknown' without blocking downstream).

v1.1 (#218-6) adds three extensions — observed footprint from name
patterns (``Straight4`` → length 4 cells), ``placement_mode`` from
corpus SQL aggregation (grid_only / free_only / mixed), and a
``connector_hint`` derived from shape + name.

Future enhancements — mesh-level geometry from the GBX itself,
cross-environment shape canonicalisation — land as classifier
version bumps. The ``classifier_version`` column lets older rows
get rebuilt cleanly when the rules change.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

from pymysql.connections import Connection

from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)

# Bump this when rule changes would invalidate existing rows.
CLASSIFIER_VERSION: str = "v1.1.0"


# Shape classes — mirrors the migration's ENUM.
SHAPE_STRAIGHT = "straight"
SHAPE_CURVE = "curve"
SHAPE_RAMP = "ramp"
SHAPE_LOOP = "loop"
SHAPE_PLATFORM = "platform"
SHAPE_SUPPORT = "support"
SHAPE_DECO = "deco"
SHAPE_START = "start"
SHAPE_CHECKPOINT = "checkpoint"
SHAPE_FINISH = "finish"
SHAPE_GATE = "gate"
SHAPE_UNKNOWN = "unknown"


# Placement-mode values (column values in block_geometry).
PLACEMENT_GRID_ONLY = "grid_only"
PLACEMENT_FREE_ONLY = "free_only"
PLACEMENT_MIXED = "mixed"
PLACEMENT_UNKNOWN = "unknown"


# Connector hints — labels the orientation of a block's drivable
# exits at rotation 0. These feed the geometry validator (#218-6
# follow-up) which checks that consecutive route cells belong to
# compatible connector types.
CONNECTOR_STRAIGHT_X = "straight_x"    # Flat run along ±X, flat Y
CONNECTOR_CURVE_XZ = "curve_xz"        # Turn in XZ plane, flat Y
CONNECTOR_SLOPE_XY = "slope_xy"        # Run along X with Y rise/fall
CONNECTOR_LOOP_Y = "loop_y"            # Full vertical loop
CONNECTOR_PLATFORM = "platform"        # Flat pad, no directional exit
CONNECTOR_ANCHOR = "anchor"            # Start / Checkpoint / Finish
CONNECTOR_NONE = ""                    # Support, deco, unknown shapes


@dataclass(frozen=True)
class BlockGeometry:
    block_family: str
    block_name: str
    shape_class: str
    surface_hint: str
    is_anchor_capable: bool
    is_deco: bool
    footprint_x: int = 1
    footprint_y: int = 1
    footprint_z: int = 1
    placement_mode: str = PLACEMENT_UNKNOWN
    connector_hint: str = CONNECTOR_NONE
    classifier_version: str = CLASSIFIER_VERSION


# ---------------------------------------------------------------------
# Shape-class inference — order matters; first-match wins. Earlier
# patterns are more specific / higher-priority.
# ---------------------------------------------------------------------

# Race-role anchors go first: a block named "PlatformTechStartLoop"
# is a Start primarily, even though it has "Loop" in its name.
_ANCHOR_PATTERNS: tuple[tuple[str, str], ...] = (
    # (substring-to-match-lowercase, shape_class)
    ("multilap", SHAPE_CHECKPOINT),
    ("linkedcheckpoint", SHAPE_CHECKPOINT),
    ("checkpoint", SHAPE_CHECKPOINT),
    ("startfinish", SHAPE_START),
    ("start", SHAPE_START),
    ("spawn", SHAPE_START),
    ("finish", SHAPE_FINISH),
    ("goal", SHAPE_FINISH),
)

# Shape-only (structural / drivable) patterns after anchors.
_SHAPE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("loop", SHAPE_LOOP),
    # "ramp" / "slope" — slopes are the commonest ramp family.
    ("slope", SHAPE_RAMP),
    ("ramp", SHAPE_RAMP),
    ("bump", SHAPE_RAMP),
    # Curves before straights since "curve" is specific.
    ("curve", SHAPE_CURVE),
    ("bend", SHAPE_CURVE),
    ("turn", SHAPE_CURVE),
    ("straight", SHAPE_STRAIGHT),
    # Support / structural (pillars, bases, etc.) — generally
    # non-drivable but we keep them visible to the classifier.
    ("pillar", SHAPE_SUPPORT),
    ("structure", SHAPE_SUPPORT),
    ("base", SHAPE_SUPPORT),
    ("support", SHAPE_SUPPORT),
    ("deadend", SHAPE_SUPPORT),
    # Gate catches anything "Gate*" that isn't an anchor.
    ("gate", SHAPE_GATE),
    # Platforms fall back to a generic "platform" class.
    ("platform", SHAPE_PLATFORM),
)

_DECO_FAMILIES: frozenset[str] = frozenset({
    "Deco", "Decoration", "Stadium", "Grass", "Water", "Trees",
})


# ---------------------------------------------------------------------
# Surface hint — family name usually carries it.
# ---------------------------------------------------------------------

_SURFACE_FAMILY_MAP: dict[str, str] = {
    "Road": "road",
    "RoadTech": "road_tech",
    "RoadDirt": "dirt",
    "RoadIce": "ice",
    "RoadBump": "road_bump",
    "Platform": "platform",
    "PlatformTech": "platform_tech",
    "PlatformDirt": "dirt",
    "PlatformIce": "ice",
    "PlatformPlastic": "plastic",
    "PlatformGrass": "grass",
    "PlatformGreen": "grass",
    "Stadium": "stadium",
    "Track": "track",
    "Wood": "wood",
    "Water": "water",
    "Magnet": "magnet",
}

# Fallback: sniff the name itself for a surface hint.
_SURFACE_NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("plastic", "plastic"),
    ("dirt", "dirt"),
    ("ice", "ice"),
    ("grass", "grass"),
    ("snow", "snow"),
    ("water", "water"),
    ("magnet", "magnet"),
    ("wood", "wood"),
    ("rally", "rally"),
    ("stunt", "stunt"),
)


# ---------------------------------------------------------------------
# Footprint inference from name suffix — #218-6.
#
# TM2020 names often carry an X-length suffix directly on the shape
# word: PlatformPlasticWallStraight4 is 4 cells along X, Slope2Straight
# is 2 cells along X. These are the multi-cell offenders the strip
# policy keeps tripping over. Curves / loops have irregular footprints
# the name doesn't encode — they stay at 1x1x1 until mesh data is
# available (M1/M2 workstream).
# ---------------------------------------------------------------------

# Shape words whose trailing digit indicates X-length. Matched against
# the name with a word boundary on either side so "Straight4" catches
# but "Straight4Gate" does not over-count. Order: most-specific first.
_FOOTPRINT_X_WORDS: tuple[str, ...] = (
    "slopestraight",
    "slope",
    "straight",
    "tilttransition",
    "wallstraight",
)

_FOOTPRINT_X_RE = re.compile(
    r"(?:" + "|".join(_FOOTPRINT_X_WORDS) + r")(\d)",
    re.IGNORECASE,
)


def _infer_footprint(shape_class: str, name: str) -> tuple[int, int, int]:
    """Best-effort (fx, fy, fz) from name suffixes.

    Returns (1, 1, 1) when the name carries no length signal. We do
    NOT try to infer non-X dimensions from names; cross-axis footprints
    need mesh-level data.
    """
    if shape_class in (
        SHAPE_CURVE, SHAPE_LOOP, SHAPE_DECO, SHAPE_UNKNOWN,
    ):
        # Curves + loops have irregular footprints; decline to guess.
        return (1, 1, 1)

    lname = name.lower()
    best_n = 1
    for m in _FOOTPRINT_X_RE.finditer(lname):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        # Names can carry two length suffixes (e.g. "Slope2Straight4") —
        # interpret the larger as the block's footprint span.
        if n > best_n:
            best_n = n

    return (best_n, 1, 1)


# ---------------------------------------------------------------------
# Connector hint from shape + name patterns — #218-6.
# ---------------------------------------------------------------------

_SHAPE_TO_CONNECTOR: dict[str, str] = {
    SHAPE_STRAIGHT: CONNECTOR_STRAIGHT_X,
    SHAPE_CURVE: CONNECTOR_CURVE_XZ,
    SHAPE_RAMP: CONNECTOR_SLOPE_XY,
    SHAPE_LOOP: CONNECTOR_LOOP_Y,
    SHAPE_PLATFORM: CONNECTOR_PLATFORM,
    SHAPE_START: CONNECTOR_ANCHOR,
    SHAPE_CHECKPOINT: CONNECTOR_ANCHOR,
    SHAPE_FINISH: CONNECTOR_ANCHOR,
    # Support / gate / deco / unknown — no connector.
}


# ---------------------------------------------------------------------
# Pure classifier
# ---------------------------------------------------------------------

def classify_block(family: str, name: str) -> BlockGeometry:
    """Infer geometry metadata for a single (family, name). Pure
    function; no DB, no side effects."""
    family = family or ""
    name = name or ""
    lname = name.lower()

    # Shape: race-role anchors first, then structural patterns.
    shape_class = SHAPE_UNKNOWN
    for sub, cls in _ANCHOR_PATTERNS:
        if sub in lname:
            shape_class = cls
            break
    if shape_class == SHAPE_UNKNOWN:
        for sub, cls in _SHAPE_PATTERNS:
            if sub in lname:
                shape_class = cls
                break

    # Deco family short-circuits to the deco shape if nothing else
    # matched (otherwise a deco block named "StadiumCurve" stays as
    # "curve" — respect the explicit geometry word).
    is_deco = family in _DECO_FAMILIES
    if is_deco and shape_class == SHAPE_UNKNOWN:
        shape_class = SHAPE_DECO

    # Surface hint — prefer the family map, fall back to the name.
    surface_hint = _SURFACE_FAMILY_MAP.get(family, "")
    if not surface_hint:
        for sub, s in _SURFACE_NAME_PATTERNS:
            if sub in lname:
                surface_hint = s
                break

    is_anchor_capable = shape_class in (
        SHAPE_START, SHAPE_CHECKPOINT, SHAPE_FINISH,
    )

    footprint_x, footprint_y, footprint_z = _infer_footprint(
        shape_class, name,
    )
    connector_hint = _SHAPE_TO_CONNECTOR.get(shape_class, CONNECTOR_NONE)

    return BlockGeometry(
        block_family=family,
        block_name=name,
        shape_class=shape_class,
        surface_hint=surface_hint,
        is_anchor_capable=is_anchor_capable,
        is_deco=is_deco,
        footprint_x=footprint_x,
        footprint_y=footprint_y,
        footprint_z=footprint_z,
        connector_hint=connector_hint,
        # placement_mode stays 'unknown' in the pure classifier —
        # it needs the corpus. ``build_block_geometry`` fills it in.
    )


# ---------------------------------------------------------------------
# DB I/O
# ---------------------------------------------------------------------

_DISTINCT_BLOCKS_SQL = """
SELECT
    block_family,
    block_type,
    SUM(CASE WHEN is_free = 0 THEN 1 ELSE 0 END) AS grid_count,
    SUM(CASE WHEN is_free = 1 THEN 1 ELSE 0 END) AS free_count
FROM block_placements
WHERE block_family IS NOT NULL AND block_family <> ''
GROUP BY block_family, block_type
"""


def _placement_mode_from_counts(
    grid_count: int, free_count: int,
) -> str:
    """Map the corpus grid/free counts to a placement_mode enum value."""
    if grid_count > 0 and free_count == 0:
        return PLACEMENT_GRID_ONLY
    if grid_count == 0 and free_count > 0:
        return PLACEMENT_FREE_ONLY
    if grid_count > 0 and free_count > 0:
        return PLACEMENT_MIXED
    return PLACEMENT_UNKNOWN


_UPSERT_GEOMETRY_SQL = """
INSERT INTO block_geometry (
    block_family, block_name,
    shape_class, surface_hint,
    is_anchor_capable, is_deco,
    footprint_x, footprint_y, footprint_z,
    placement_mode, connector_hint,
    classifier_version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    shape_class = VALUES(shape_class),
    surface_hint = VALUES(surface_hint),
    is_anchor_capable = VALUES(is_anchor_capable),
    is_deco = VALUES(is_deco),
    footprint_x = VALUES(footprint_x),
    footprint_y = VALUES(footprint_y),
    footprint_z = VALUES(footprint_z),
    placement_mode = VALUES(placement_mode),
    connector_hint = VALUES(connector_hint),
    classifier_version = VALUES(classifier_version),
    updated_at = CURRENT_TIMESTAMP(6)
"""


@dataclass
class BuildReport:
    distinct_blocks_seen: int = 0
    rows_written: int = 0
    shape_breakdown: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.shape_breakdown is None:
            self.shape_breakdown = {}


def build_block_geometry(
    conn: Connection, *, families: Iterable[str] | None = None,
) -> BuildReport:
    """Scan block_placements for distinct (family, name) combos, run
    the classifier, upsert into ``block_geometry``.

    v1.1 (#218-6) derives ``placement_mode`` from the per-block
    grid/free counts the aggregation SQL already returns — no second
    round-trip, the GROUP BY does it in one pass.

    ``families`` is an optional filter for smoke runs.
    """
    with cursor(conn) as cur:
        sql = _DISTINCT_BLOCKS_SQL
        params: tuple = ()
        if families:
            fam_list = list(families)
            placeholders = ",".join(["%s"] * len(fam_list))
            # GROUP BY already pushed past the WHERE so we splice the
            # IN-filter in ahead of GROUP BY. The SQL template makes
            # this readable enough without a query builder.
            sql = sql.replace(
                "GROUP BY block_family, block_type",
                f"AND block_family IN ({placeholders}) "
                "GROUP BY block_family, block_type",
            )
            params = tuple(fam_list)
        cur.execute(sql, params)
        distinct = cur.fetchall()

    report = BuildReport()
    report.distinct_blocks_seen = len(distinct)

    rows: list[tuple] = []
    for family, name, grid_count, free_count in distinct:
        geom = classify_block(str(family), str(name))
        placement_mode = _placement_mode_from_counts(
            int(grid_count or 0), int(free_count or 0),
        )
        report.shape_breakdown[geom.shape_class] = (
            report.shape_breakdown.get(geom.shape_class, 0) + 1
        )
        rows.append((
            geom.block_family, geom.block_name,
            geom.shape_class, geom.surface_hint,
            int(geom.is_anchor_capable), int(geom.is_deco),
            int(geom.footprint_x), int(geom.footprint_y), int(geom.footprint_z),
            placement_mode, geom.connector_hint,
            geom.classifier_version,
        ))

    if rows:
        with cursor(conn) as cur:
            cur.executemany(_UPSERT_GEOMETRY_SQL, rows)
        conn.commit()
        report.rows_written = len(rows)

    _LOG.info(
        "build_block_geometry: distinct=%d rows_written=%d breakdown=%s",
        report.distinct_blocks_seen, report.rows_written,
        {k: v for k, v in sorted(report.shape_breakdown.items(),
                                 key=lambda p: -p[1])[:5]},
    )
    return report


# ---------------------------------------------------------------------
# Read path for downstream consumers.
# ---------------------------------------------------------------------

_FETCH_SQL = """
SELECT block_family, block_name, shape_class, surface_hint,
       is_anchor_capable, is_deco,
       footprint_x, footprint_y, footprint_z,
       placement_mode, connector_hint,
       classifier_version
FROM block_geometry
WHERE block_family = %s AND block_name = %s
"""


def fetch_geometry(
    conn: Connection, family: str, name: str,
) -> BlockGeometry | None:
    with cursor(conn) as cur:
        cur.execute(_FETCH_SQL, (family, name))
        r = cur.fetchone()
    if r is None:
        return None
    return BlockGeometry(
        block_family=str(r[0]), block_name=str(r[1]),
        shape_class=str(r[2]), surface_hint=str(r[3]),
        is_anchor_capable=bool(r[4]), is_deco=bool(r[5]),
        footprint_x=int(r[6]), footprint_y=int(r[7]), footprint_z=int(r[8]),
        placement_mode=str(r[9]), connector_hint=str(r[10]),
        classifier_version=str(r[11]),
    )
