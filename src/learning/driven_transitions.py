"""Mine observed transitions and run lengths from driven paths.

Two outputs, deliberately separated because they answer different
questions and the §9 failure came from conflating them:

* **Transitions between DISTINCT block types** -- which block follows
  which, with relative rotation and local-frame delta. Safe to use as
  a preference between candidate successors.
* **Run lengths within one block type** -- how long real routes stay on
  the same type before changing. The geometric sequence prior failed
  precisely because same-type runs dominated it: rewarding "A follows
  A" per step rewards unbounded repetition. The driven data answers the
  question properly -- runs have a DISTRIBUTION, and a walker mechanism
  can target it instead of multiplying into it.

Analysis first, consumption second: `analyse()` reports the structural
numbers (same-type share above all) that decide whether per-step
weighting is safe at all.
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

_LOG = logging.getLogger(__name__)

MINER_VERSION = "driven_transitions-0.1"


def _rotate_local(dx: int, dz: int, from_rot: int) -> tuple[int, int]:
    """World XZ delta into the from-block's local frame (undo rotation).

    Same sense as the calibrated rotation machinery: one ring step per
    quarter-turn, (vx, vz) -> (-vz, vx) for d=1.
    """
    d = (4 - (from_rot % 4)) % 4
    for _ in range(d):
        dx, dz = -dz, dx
    return dx, dz


@dataclass
class MinedTransition:
    from_type: str
    to_type: str
    rel_rot: int | None
    dx: int
    dy: int
    dz: int
    link: str


def mine(conn, *, extractor_version: str = "driven_path-0.1"):
    """Yield (map-deduped) transitions plus per-type run lengths."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.replay_id, v.visit_index, v.state, v.placement_id,
                   p.block_type, p.x, p.y, p.z, p.rotation, p.is_free,
                   p.abs_x, p.abs_y, p.abs_z
              FROM driven_block_visits v
              LEFT JOIN block_placements p ON p.id = v.placement_id
             WHERE v.extractor_version = %s
             ORDER BY v.replay_id, v.visit_index
            """,
            (extractor_version,),
        )
        rows = cur.fetchall()

    counts: dict[tuple, Counter] = defaultdict(Counter)   # key -> per-replay n
    run_lengths: dict[str, Counter] = defaultdict(Counter)

    by_replay: dict[int, list] = defaultdict(list)
    for r in rows:
        by_replay[r[0]].append(r)

    for replay_id, visits in by_replay.items():
        prev_block = None       # (type, x, y, z, rot, is_free)
        pending_link = "contact"
        run_type, run_len = None, 0
        for v in visits:
            state = v[2]
            if state == "airborne":
                pending_link = "jump"
                continue
            if state == "off_surface":
                # jump + gap in one stretch: gap wins, because an
                # off-surface segment breaks any claim the blocks join.
                pending_link = "gap"
                continue
            btype = v[4]
            if btype is None:
                continue

            # Run-length bookkeeping counts CHANGES of type, so an
            # A->A transition between two distinct placements extends
            # the run rather than emitting a self-transition row.
            if btype == run_type:
                run_len += 1
            else:
                if run_type is not None:
                    run_lengths[run_type][run_len] += 1
                run_type, run_len = btype, 1

            if prev_block is not None:
                f_type, fx, fy, fz, f_rot, f_free = prev_block
                t_free = bool(v[9])
                if not f_free and not t_free and v[5] is not None and fx is not None:
                    wdx, wdy, wdz = v[5] - fx, v[6] - fy, v[7] - fz
                    ldx, ldz = _rotate_local(wdx, wdz, f_rot or 0)
                    rel = ((v[8] or 0) - (f_rot or 0)) % 4
                else:
                    # Free endpoint: keep the transition, drop the
                    # frame-dependent fields to a coarse marker.
                    ldx = wdy = ldz = 0
                    wdy = 0
                    rel = None
                if f_type != btype or pending_link != "contact":
                    key = (f_type, btype, rel, ldx, wdy, ldz, pending_link)
                    counts[key][replay_id] += 1
            prev_block = (btype, v[5], v[6], v[7], v[8], bool(v[9]))
            pending_link = "contact"
        if run_type is not None:
            run_lengths[run_type][run_len] += 1

    mined = [
        (MinedTransition(*key), sum(per.values()), len(per))
        for key, per in counts.items()
    ]
    return mined, run_lengths


def persist(conn, mined, *, version: str = MINER_VERSION) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM driven_transitions WHERE extractor_version = %s",
            (version,),
        )
        cur.executemany(
            """
            INSERT INTO driven_transitions (
                extractor_version, from_type, to_type, rel_rot,
                dx, dy, dz, link, n_transitions, n_maps,
                created_by_version
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            [
                (version, t.from_type, t.to_type, t.rel_rot,
                 t.dx, t.dy, t.dz, t.link, n, nm, version)
                for t, n, nm in mined
            ],
        )
    conn.commit()
    return len(mined)


def analyse(mined, run_lengths) -> dict[str, Any]:
    """The numbers that decide how the walker may consume this."""
    total = sum(n for _, n, _ in mined)
    same = sum(n for t, n, _ in mined if t.from_type == t.to_type)
    by_link = Counter()
    for t, n, _ in mined:
        by_link[t.link] += n

    # Run-length distribution, aggregated.
    agg = Counter()
    for c in run_lengths.values():
        agg.update(c)
    runs_total = sum(agg.values())
    run_hist = {str(k): v for k, v in sorted(agg.items())[:12]}

    # Successor entropy proxy: how concentrated is the next-type
    # distribution per from_type (contact links, distinct types only)?
    succ: dict[str, Counter] = defaultdict(Counter)
    for t, n, _ in mined:
        if t.link == "contact" and t.from_type != t.to_type:
            succ[t.from_type][t.to_type] += n
    top1 = [
        max(c.values()) / sum(c.values())
        for c in succ.values() if sum(c.values()) >= 10
    ]

    return {
        "transitions_total": total,
        "distinct_rows": len(mined),
        "same_type_share": round(same / max(1, total), 4),
        "by_link": dict(by_link),
        "run_length_hist": run_hist,
        "run_length_mean": round(
            sum(k * v for k, v in agg.items()) / max(1, runs_total), 2),
        "from_types_with_10plus_contact_successions": len(top1),
        "top1_successor_share_mean": round(
            sum(top1) / max(1, len(top1)), 3) if top1 else None,
    }


def export_artifact(mined, run_lengths, analysis, out_path) -> None:
    """Generator-consumable artifact: transitions + run-length prior."""
    doc = {
        "version": MINER_VERSION,
        "analysis": analysis,
        "run_lengths": {
            k: {str(a): b for a, b in sorted(c.items())}
            for k, c in run_lengths.items()
        },
        "transitions": [
            {
                "from": t.from_type, "to": t.to_type, "rel_rot": t.rel_rot,
                "d": [t.dx, t.dy, t.dz], "link": t.link,
                "n": n, "maps": nm,
            }
            for t, n, nm in sorted(mined, key=lambda r: -r[1])
        ],
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    _LOG.info("wrote %s", out_path)
