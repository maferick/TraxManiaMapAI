"""Export driven-block sequences as construction token streams.

The transform from observed ground truth to trainable target. Input is
``driven_block_visits`` (the ordered blocks a validation ghost actually
rode); output is one token sequence per capture:

* ``PLACE <block> <dx> <dy> <dz> <rot>`` -- first visit to a placement.
  Deltas are grid cells relative to the PREVIOUS placed block, so the
  vocabulary is small and translation-invariant; the first block is the
  origin. Rotation is absolute (0-3), because rotation identity matters
  (the grammar work measured the cost of dropping it).
* ``REVISIT <n>`` -- the route returns to a block already placed;
  ``n`` counts back in first-visit order (1 = the most recent). A
  bounded reference, per the owner design, so revisits never inflate
  the vocabulary.
* ``JUMP`` -- an AIRBORNE stretch. Route semantics, not construction:
  the car was on nothing, and the next PLACE is reached through the
  air. Trainers must not treat JUMP as a placement target.
* ``GAP`` -- an OFF_SURFACE stretch (terrain, zone interior, item
  surface, matcher hole). Same rule: context, never a target.

What is deliberately NOT here:

* No baked blocks, no derived surfaces, no items. ``driven_block_visits``
  only references ``block_placements`` rows, which are editor-placeable
  by construction, so the non-placeable vocabulary problem
  (``PLACE OpenTechRoadFC``) cannot arise in this export.
* No denoising of A-B-A edge bounces. They are exported as REVISITs and
  measured; deciding whether they are noise is a modelling call that
  should be made looking at the numbers, not silently inside the
  exporter.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

_LOG = logging.getLogger(__name__)

EXPORT_VERSION = "construction-0.1"

# Grid geometry for quantizing free placements, same constants as the
# matcher.
CELL_SIZE_M = 32.0
LEVEL_HEIGHT_M = 8.0
GROUND_ROW = 9
GROUND_ABS_Y_M = 8.0


@dataclass
class ExportStats:
    sequences: int = 0
    tokens: int = 0
    places: int = 0
    revisits: int = 0
    fast_revisits: int = 0        # revisit visits shorter than 500ms
    jumps: int = 0
    gaps: int = 0
    skipped: int = 0
    vocabulary: set = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequences": self.sequences,
            "tokens": self.tokens,
            "places": self.places,
            "revisits": self.revisits,
            "fast_revisits": self.fast_revisits,
            "jumps": self.jumps,
            "gaps": self.gaps,
            "skipped": self.skipped,
            "vocabulary_size": len(self.vocabulary),
        }


def _cell(p: dict[str, Any]) -> tuple[int, int, int] | None:
    """Grid cell for a placement row, quantizing free placements."""
    if p.get("x") is not None:
        return (p["x"], p["y"], p["z"])
    if p.get("abs_x") is not None:
        return (
            math.floor(p["abs_x"] / CELL_SIZE_M),
            math.floor(GROUND_ROW + (p["abs_y"] - GROUND_ABS_Y_M) / LEVEL_HEIGHT_M),
            math.floor(p["abs_z"] / CELL_SIZE_M),
        )
    return None


def visits_to_tokens(
    visits: Sequence[dict[str, Any]],
    placements: dict[int, dict[str, Any]],
    stats: ExportStats | None = None,
) -> list[dict[str, Any]]:
    """One capture's visits -> token list. Pure transform, no I/O."""
    stats = stats if stats is not None else ExportStats()
    tokens: list[dict[str, Any]] = []
    placed_order: list[int] = []          # placement ids in first-visit order
    placed_set: dict[int, int] = {}       # placement id -> index in placed_order
    prev_cell: tuple[int, int, int] | None = None

    for v in visits:
        state = v["state"]
        if state == "airborne":
            # Collapse runs: two adjacent JUMPs carry no more information
            # than one.
            if not tokens or tokens[-1].get("op") != "JUMP":
                tokens.append({"op": "JUMP"})
                stats.jumps += 1
            continue
        if state == "off_surface":
            if not tokens or tokens[-1].get("op") != "GAP":
                tokens.append({"op": "GAP"})
                stats.gaps += 1
            continue

        pid = v["placement_id"]
        p = placements.get(pid)
        if p is None:
            stats.skipped += 1
            continue

        if pid in placed_set:
            back = len(placed_order) - placed_set[pid]
            tokens.append({"op": "REVISIT", "back": back})
            stats.revisits += 1
            if (v["exit_ms"] - v["enter_ms"]) < 500:
                stats.fast_revisits += 1
            # A revisit moves the cursor: subsequent deltas are relative
            # to where the car actually is, not where it last built.
            c = _cell(p)
            if c is not None:
                prev_cell = c
            continue

        c = _cell(p)
        if c is None:
            stats.skipped += 1
            continue
        if prev_cell is None:
            d = (0, 0, 0)
        else:
            d = (c[0] - prev_cell[0], c[1] - prev_cell[1], c[2] - prev_cell[2])
        tokens.append({
            "op": "PLACE",
            "block": p["block_type"],
            "d": list(d),
            "rot": int(p.get("rotation") or 0),
            "free": bool(p.get("is_free")),
        })
        stats.places += 1
        stats.vocabulary.add(p["block_type"])
        placed_set[pid] = len(placed_order)
        placed_order.append(pid)
        prev_cell = c

    stats.tokens += len(tokens)
    return tokens


def export_sequences(conn, out_path, *, extractor_version: str) -> ExportStats:
    """Write one JSONL line per capture: header + token stream."""
    stats = ExportStats()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT replay_id FROM driven_block_visits "
            "WHERE extractor_version = %s",
            (extractor_version,),
        )
        replay_ids = [r[0] for r in cur.fetchall()]

    with open(out_path, "w", encoding="utf-8") as fh:
        for replay_id in replay_ids:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT visit_index, state, placement_id, enter_ms, exit_ms
                      FROM driven_block_visits
                     WHERE replay_id = %s AND extractor_version = %s
                     ORDER BY visit_index
                    """,
                    (replay_id, extractor_version),
                )
                visits = [
                    {"visit_index": r[0], "state": r[1], "placement_id": r[2],
                     "enter_ms": r[3], "exit_ms": r[4]}
                    for r in cur.fetchall()
                ]
                pids = [v["placement_id"] for v in visits
                        if v["placement_id"] is not None]
                placements: dict[int, dict[str, Any]] = {}
                if pids:
                    cur.execute(
                        "SELECT id, block_type, x, y, z, rotation, is_free, "
                        "abs_x, abs_y, abs_z FROM block_placements "
                        "WHERE id IN (%s)" % ",".join(["%s"] * len(pids)),
                        pids,
                    )
                    for r in cur.fetchall():
                        placements[r[0]] = {
                            "block_type": r[1], "x": r[2], "y": r[3],
                            "z": r[4], "rotation": r[5],
                            "is_free": bool(r[6]),
                            "abs_x": float(r[7]) if r[7] is not None else None,
                            "abs_y": float(r[8]) if r[8] is not None else None,
                            "abs_z": float(r[9]) if r[9] is not None else None,
                        }
                cur.execute(
                    """
                    SELECT r.source_replay_id, r.finish_time_ms, m.title,
                           m.length_estimate_ms
                      FROM replays r JOIN maps m ON m.id = r.map_id
                     WHERE r.id = %s
                    """,
                    (replay_id,),
                )
                meta = cur.fetchone()

            tokens = visits_to_tokens(visits, placements, stats)
            if not tokens:
                continue
            stats.sequences += 1
            fh.write(json.dumps({
                "version": EXPORT_VERSION,
                "extractor_version": extractor_version,
                "replay_id": replay_id,
                "map_uid": meta[0] if meta else None,
                "finish_time_ms": meta[1] if meta else None,
                "title": meta[2] if meta else None,
                "tokens": tokens,
            }, ensure_ascii=False) + "\n")

    _LOG.info(
        "exported %d sequence(s), %d tokens (%d PLACE, %d REVISIT of which "
        "%d fast, %d JUMP, %d GAP), vocabulary %d block types, %d skipped",
        stats.sequences, stats.tokens, stats.places, stats.revisits,
        stats.fast_revisits, stats.jumps, stats.gaps,
        len(stats.vocabulary), stats.skipped,
    )
    return stats
