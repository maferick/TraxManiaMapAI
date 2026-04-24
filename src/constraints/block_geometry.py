"""Phase 2 #218-3 — block-geometry catalogue.

Per-(block_family, block_name) metadata used as a soft input to the
generation-time pattern/geometry compatibility score. Scope per
project-218 doc: soft signal only; frequency + geometry never
override traversability evidence.

The v1 classifier infers shape / surface / role from the block_name
string via pattern matching. This gets 99% of the corpus right on
Nadeo-shipped blocks and is brittle on community custom blocks
(those get shape_class='unknown' without blocking downstream).

Future enhancements — mesh-level geometry from the GBX itself,
cross-environment shape canonicalisation — land as classifier
version bumps. The ``classifier_version`` column lets older rows
get rebuilt cleanly when the rules change.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from pymysql.connections import Connection

from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)

# Bump this when rule changes would invalidate existing rows.
CLASSIFIER_VERSION: str = "v1.0.0"


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

    return BlockGeometry(
        block_family=family,
        block_name=name,
        shape_class=shape_class,
        surface_hint=surface_hint,
        is_anchor_capable=is_anchor_capable,
        is_deco=is_deco,
    )


# ---------------------------------------------------------------------
# DB I/O
# ---------------------------------------------------------------------

_DISTINCT_BLOCKS_SQL = """
SELECT DISTINCT block_family, block_type
FROM block_placements
WHERE is_free = 0
  AND block_family IS NOT NULL AND block_family <> ''
"""


_UPSERT_GEOMETRY_SQL = """
INSERT INTO block_geometry (
    block_family, block_name,
    shape_class, surface_hint,
    is_anchor_capable, is_deco,
    footprint_x, footprint_y, footprint_z,
    classifier_version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    shape_class = VALUES(shape_class),
    surface_hint = VALUES(surface_hint),
    is_anchor_capable = VALUES(is_anchor_capable),
    is_deco = VALUES(is_deco),
    footprint_x = VALUES(footprint_x),
    footprint_y = VALUES(footprint_y),
    footprint_z = VALUES(footprint_z),
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
    the classifier, upsert into ``block_geometry``. ``families`` is
    an optional filter for smoke runs."""
    with cursor(conn) as cur:
        sql = _DISTINCT_BLOCKS_SQL
        params: tuple = ()
        if families:
            fam_list = list(families)
            placeholders = ",".join(["%s"] * len(fam_list))
            sql += f" AND block_family IN ({placeholders})"
            params = tuple(fam_list)
        cur.execute(sql, params)
        distinct = cur.fetchall()

    report = BuildReport()
    report.distinct_blocks_seen = len(distinct)

    rows: list[tuple] = []
    for family, name in distinct:
        geom = classify_block(str(family), str(name))
        report.shape_breakdown[geom.shape_class] = (
            report.shape_breakdown.get(geom.shape_class, 0) + 1
        )
        rows.append((
            geom.block_family, geom.block_name,
            geom.shape_class, geom.surface_hint,
            int(geom.is_anchor_capable), int(geom.is_deco),
            int(geom.footprint_x), int(geom.footprint_y), int(geom.footprint_z),
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
       footprint_x, footprint_y, footprint_z, classifier_version
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
        classifier_version=str(r[9]),
    )
