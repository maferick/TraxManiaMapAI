"""Unit tests for the observed placement-grammar miner.

The point of this stage is what it does NOT require: no clip match, no
adjacency, no shared family. Each test below pins one thing the
clip-derived extractor throws away and a real corpus map contains.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.catalogue.loader import load_catalogue, rotate_vector
from src.constraints import placement_grammar as pg_mod
from src.constraints.placement_grammar import (
    _map_pairs,
    _OffsetTable,
    _unpack_key,
    build_grammar,
)

CLIP = "RoadTechFC"


def _block(block_id: str, clips: dict[str, list[str]] | None = None) -> dict:
    return {
        "type": "block", "id": block_id, "name": block_id,
        "page": "RoadTech/Main/", "waypoint": "None", "is_pillar": False,
        "collection": "Stadium2020",
        "variants": [{
            "kind": "ground", "index": 0, "size": [1, 1, 1],
            "units": [{
                "offset": [0, 0, 0], "underground": False,
                "terrain_modifier": "", "surface": "",
                "clips": clips or {},
            }],
        }],
    }


@pytest.fixture()
def catalogue(tmp_path):
    records = [
        _block("Straight", {"n": [CLIP], "s": [CLIP]}),
        _block("Curve", {"n": [CLIP], "e": [CLIP]}),
        # No clips at all — GateCheckpoint is exactly this shape.
        _block("Gate"),
    ]
    path = tmp_path / "catalogue.ndjson"
    lines = [json.dumps({"type": "meta", "schema": "block_catalogue_v1"})]
    lines += [json.dumps(r) for r in records]
    path.write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "catalogue.done.json").write_text("{}", encoding="utf-8")
    return load_catalogue(path, collection="Stadium2020")


def _pairs(placements, radius_xz=1, radius_y=1):
    """Run the vectorised join over a small hand-written map."""
    table = _OffsetTable(radius_xz, radius_y)
    names = sorted({p[0] for p in placements})
    ids = {n: i for i, n in enumerate(names)}
    arr = lambda i: np.array([p[i] for p in placements], dtype=np.int64)
    keys, counts = _map_pairs(
        np.array([ids[p[0]] for p in placements], dtype=np.int64),
        arr(1), arr(2), arr(3), arr(4), table,
    )
    out = {}
    for key, count in zip(keys.tolist(), counts.tolist()):
        ta, tb, rel, oi = _unpack_key(key, len(table))
        out[(names[ta], names[tb], table.offsets[oi], rel)] = count
    return out


class TestNeighbourJoin:
    def test_offsets_are_stored_in_block_a_frame(self):
        """The same shape learned at any heading must give one key.

        Otherwise the grammar has to observe every pattern four times
        before it generalises, and the four counts never merge.
        """
        seen = []
        for turn in range(4):
            placed = [
                ("Straight", turn, 0, 0, 0),
                ("Curve", (1 + turn) % 4, *rotate_vector((1, 0, 0), turn)),
            ]
            seen.append(_pairs(placed))
        assert all(s == seen[0] for s in seen)
        assert seen[0][("Straight", "Curve", (1, 0, 0), 1)] == 1

    def test_two_blocks_in_one_cell_pair_at_zero_offset(self):
        """Corpus map 25192 stacks two blocks at (25, 10, 20)."""
        got = _pairs([
            ("Straight", 0, 5, 9, 5),
            ("Gate", 0, 5, 9, 5),
        ])
        assert got[("Straight", "Gate", (0, 0, 0), 0)] == 1
        assert got[("Gate", "Straight", (0, 0, 0), 0)] == 1

    def test_a_block_does_not_pair_with_itself(self):
        got = _pairs([("Straight", 0, 5, 9, 5)])
        assert got == {}

    def test_gap_pairs_survive(self):
        """A jump: no shared cell boundary, so no clip can match."""
        got = _pairs(
            [("Straight", 0, 0, 9, 0), ("Straight", 0, 0, 9, 3)],
            radius_xz=3, radius_y=1,
        )
        assert got[("Straight", "Straight", (0, 0, 3), 0)] == 1

    def test_negative_coordinates(self):
        """Coordinate packing must not leak between fields."""
        got = _pairs([
            ("Straight", 0, -4, 10, -2),
            ("Curve", 0, -3, 10, -2),
        ])
        assert got == {
            ("Straight", "Curve", (1, 0, 0), 0): 1,
            ("Curve", "Straight", (-1, 0, 0), 0): 1,
        }


def _wire_db(monkeypatch, map_ids, placements_by_map):
    """Stub the cursor: map list first, then one rowset per map."""
    upserts: list[tuple] = []
    results = iter(
        [[(m,) for m in map_ids]] + [placements_by_map[m] for m in map_ids]
    )
    cur = MagicMock()
    cur.execute.side_effect = lambda sql, params=None: None
    cur.executemany.side_effect = (
        lambda sql, rows: upserts.extend(rows)
        if "block_placement_grammar" in sql else None
    )
    cur.fetchall.side_effect = lambda: next(results)
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    ctx.__exit__.return_value = False
    monkeypatch.setattr(pg_mod, "cursor", lambda conn: ctx)
    return upserts


class TestBuildGrammar:
    def test_clipless_pair_is_recorded_but_flagged(self, catalogue, monkeypatch):
        """The whole point: no clip, still a row."""
        upserts = _wire_db(
            monkeypatch, [1],
            {1: [("Straight", 0, 0, 9, 0), ("Gate", 0, 0, 9, 0)]},
        )
        build_grammar(
            MagicMock(), catalogue, radius_xz=1, radius_y=1, min_map_count=1,
        )
        rows = {(u[1], u[2]): u for u in upserts}
        assert ("Straight", "Gate") in rows
        assert rows[("Straight", "Gate")][8] == 0  # clip_matched

    def test_clip_matched_pairs_are_flagged(self, catalogue, monkeypatch):
        # Straight r0 at z and z+1: n-port meets the other's s-port.
        upserts = _wire_db(
            monkeypatch, [1],
            {1: [("Straight", 0, 0, 9, 0), ("Straight", 0, 0, 9, 1)]},
        )
        build_grammar(
            MagicMock(), catalogue, radius_xz=1, radius_y=1, min_map_count=1,
        )
        forward = [u for u in upserts if (u[3], u[4], u[5]) == (0, 0, 1)]
        assert len(forward) == 1
        assert forward[0][8] == 1

    def test_min_map_count_drops_the_single_map_tail(
        self, catalogue, monkeypatch
    ):
        shared = [("Straight", 0, 0, 9, 0), ("Straight", 0, 0, 9, 1)]
        upserts = _wire_db(
            monkeypatch, [1, 2],
            {1: list(shared), 2: shared + [("Curve", 0, 1, 9, 0)]},
        )
        report = build_grammar(
            MagicMock(), catalogue, radius_xz=1, radius_y=1, min_map_count=2,
        )
        assert report.maps_seen == 2
        blocks = {(u[1], u[2]) for u in upserts}
        assert ("Straight", "Straight") in blocks
        assert ("Straight", "Curve") not in blocks  # only in map 2
        assert all(u[11] == pg_mod.STAGE_VERSION for u in upserts)

    def test_counts_separate_volume_from_breadth(self, catalogue, monkeypatch):
        """pair_count is volume, map_count is breadth.

        Priors weight on breadth: one map with a 40-block straight run
        must not outvote forty maps that each use the pattern once.
        """
        run = [("Straight", 0, 0, 9, i) for i in range(4)]
        upserts = _wire_db(
            monkeypatch, [1, 2],
            {1: list(run), 2: [("Straight", 0, 0, 9, 0),
                               ("Straight", 0, 0, 9, 1)]},
        )
        build_grammar(
            MagicMock(), catalogue, radius_xz=1, radius_y=1, min_map_count=1,
        )
        forward = [u for u in upserts if (u[3], u[4], u[5]) == (0, 0, 1)][0]
        assert forward[9] == 4  # pair_count: 3 in map 1, 1 in map 2
        assert forward[10] == 2  # map_count
