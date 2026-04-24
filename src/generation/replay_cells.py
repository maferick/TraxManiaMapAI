"""Replay-touched-cell loader (follow-up to the preemit hook).

Feeds :mod:`src.generation.jump_validator` its
``replay_touched_cells`` parameter so the classifier can return
``supported_by_replay`` — the CLAUDE.md authoritative-evidence class.

Data source (v0 proxy)
----------------------

The repo doesn't store per-frame replay position samples in MariaDB
— replays live as sidecar files referenced by the ``replays`` table
(``path``, ``breadcrumbs_path``). Exposing those samples to Python
requires the future OpenPlanet telemetry workstream.

What we DO have is enumerated ``route_corridors`` paths, each with
a ``corridor_confidence`` column that aggregates the four evidence
signals (including ``path_support_count`` = number of replays that
confirmed the path). A corridor whose ``corridor_confidence`` is
positive was backed by at least some replay observation; its
``path_cells`` therefore describe cells replays have driven through.

This module returns the UNION of ``path_cells`` across qualifying
corridors per map. That's a proxy, not ground truth — a corridor
confirmed by a single replay reports its full path even though the
driver may have deviated laterally. For the jump validator's
purposes this is acceptable: the classifier asks "did a replay
cross this transition?", and a corridor-backed cell is exactly
that signal at corridor resolution. The v1 upgrade (driven from
OpenPlanet per-frame data) will tighten the resolution.

Behaviour when no corridor data is available (fresh DB, maps
without scored corridors): returns an empty set. Downstream
validators fall back to geometry-only classification.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pymysql.connections import Connection

from src.generation.geom_validator import Cell
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)


# Conservative default: any corridor with a positive confidence score
# contributes its cells. Callers that want higher-confidence-only
# signal tighten this knob.
_DEFAULT_MIN_CONFIDENCE: float = 0.0


_CELLS_SQL = """
SELECT path_cells
FROM route_corridors
WHERE map_id = %s
  AND corridor_confidence IS NOT NULL
  AND corridor_confidence > %s
"""


def _parse_path_cells(raw: Any) -> list[Cell]:
    """Decode the LONGTEXT JSON blob that ``path_cells`` stores."""
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (TypeError, json.JSONDecodeError):
        return []
    out: list[Cell] = []
    if not isinstance(data, list):
        return out
    for entry in data:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            continue
        try:
            out.append((int(entry[0]), int(entry[1]), int(entry[2])))
        except (TypeError, ValueError):
            continue
    return out


def load_replay_touched_cells(
    conn: Connection,
    *,
    map_id: int,
    min_corridor_confidence: float = _DEFAULT_MIN_CONFIDENCE,
) -> set[Cell]:
    """Union of cells that confidence-backed corridors on this map cover.

    ``min_corridor_confidence`` excludes corridors below the given
    confidence threshold. The default (``0.0``) keeps any
    positive-confidence corridor; pass something like ``0.3`` to
    restrict to high-confidence paths only.

    Returns an empty set when no qualifying corridors exist — which
    is the documented signal to :func:`validate_jumps` that no
    replay evidence is available (geometry-only classification).
    """
    cells: set[Cell] = set()
    qualifying = 0
    with cursor(conn) as cur:
        cur.execute(_CELLS_SQL, (map_id, min_corridor_confidence))
        for (raw,) in cur.fetchall():
            qualifying += 1
            for cell in _parse_path_cells(raw):
                cells.add(cell)
    _LOG.info(
        "load_replay_touched_cells: map_id=%d corridors>=%.2f: %d "
        "unique_cells=%d",
        map_id, min_corridor_confidence, qualifying, len(cells),
    )
    return cells
