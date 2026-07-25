"""Unit tests for the face-aware transition extractor.

Uses the calibrated rotation frame (see src/catalogue/loader.py):
face n looks at +z, one rotation step sends a local n-face to e (-x).
Fixtures mirror real RoadTech clip topology.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.catalogue.loader import load_catalogue
from src.constraints import face_transitions as ft_mod
from src.constraints.face_transitions import (
    FaceTransitionReport,
    _TransitionKey,
    build_face_transitions,
)

CLIP = "RoadTechFC"


def _block(block_id: str, clips: dict[str, list[str]]) -> dict:
    return {
        "type": "block", "id": block_id, "name": block_id,
        "page": "RoadTech/Main/", "waypoint": "None", "is_pillar": False,
        "variants": [{
            "kind": "ground", "index": 0, "size": [1, 1, 1],
            "units": [{
                "offset": [0, 0, 0], "underground": False,
                "terrain_modifier": "", "surface": "", "clips": clips,
            }],
        }],
    }


@pytest.fixture()
def catalogue(tmp_path):
    records = [
        _block("Straight", {"n": [CLIP], "s": [CLIP]}),
        _block("Curve", {"n": [CLIP], "e": [CLIP]}),
    ]
    path = tmp_path / "catalogue.ndjson"
    lines = [json.dumps({"type": "meta", "schema": "block_catalogue_v1"})]
    lines += [json.dumps(r) for r in records]
    path.write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "catalogue.done.json").write_text("{}", encoding="utf-8")
    return load_catalogue(path)


def _wire_db(monkeypatch, map_rows, placements_by_map):
    """Stub the cursor: first execute returns the map list, then one
    placements rowset per map, then upserts are captured."""
    upserts: list[tuple] = []
    rowsets = [map_rows] + [placements_by_map[m[0]] for m in map_rows]
    results = iter(rowsets)
    cur = MagicMock()

    def execute_impl(sql, params=None):
        if "INSERT INTO block_face_transitions" in sql:
            upserts.append(params)

    def fetchall_impl():
        return next(results)

    cur.execute.side_effect = execute_impl
    cur.fetchall.side_effect = fetchall_impl
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    ctx.__exit__.return_value = False
    monkeypatch.setattr(ft_mod, "cursor", lambda conn: ctx)
    return upserts


class TestBuildFaceTransitions:
    def test_facing_straights_count_both_directions(self, catalogue, monkeypatch):
        # Straight r0 at z=10 and z=11: n-port of the first meets the
        # s-port of the second on the shared boundary.
        upserts = _wire_db(
            monkeypatch,
            map_rows=[(1, "Stadium")],
            placements_by_map={1: [
                ("Straight", 0, 10, 9, 10),
                ("Straight", 0, 10, 9, 11),
            ]},
        )
        report = build_face_transitions(MagicMock(), catalogue)
        assert report.transitions_counted == 2  # A->B and B->A
        assert len(upserts) == 1  # same key both ways (rel_rotation 0)
        sig, a, b, clip, rel, env, count, mapc, ver = upserts[0]
        assert (a, b, clip, rel) == ("Straight", "Straight", CLIP, 0)
        assert count == 2 and mapc == 1

    def test_straight_into_curve_rel_rotation(self, catalogue, monkeypatch):
        # Curve r2 exposes its n-clip on world face s — meeting the
        # straight's n-port. rel_rotation must be 2 both ways.
        upserts = _wire_db(
            monkeypatch,
            map_rows=[(1, "Stadium")],
            placements_by_map={1: [
                ("Straight", 0, 10, 9, 10),
                ("Curve", 2, 10, 9, 11),
            ]},
        )
        report = build_face_transitions(MagicMock(), catalogue)
        assert report.transitions_counted == 2
        keys = {(u[1], u[2], u[4]) for u in upserts}
        assert keys == {("Straight", "Curve", 2), ("Curve", "Straight", 2)}

    def test_side_by_side_straights_do_not_transition(self, catalogue, monkeypatch):
        # Parallel straights: adjacent cells but no facing ports.
        # Cell adjacency would count this; clip matching must not.
        upserts = _wire_db(
            monkeypatch,
            map_rows=[(1, "Stadium")],
            placements_by_map={1: [
                ("Straight", 0, 10, 9, 10),
                ("Straight", 0, 11, 9, 10),
            ]},
        )
        report = build_face_transitions(MagicMock(), catalogue)
        assert report.transitions_counted == 0
        assert upserts == []

    def test_unknown_block_is_counted_not_fatal(self, catalogue, monkeypatch):
        _wire_db(
            monkeypatch,
            map_rows=[(1, "Stadium")],
            placements_by_map={1: [
                ("CommunityCustomBlock", 0, 10, 9, 10),
                ("Straight", 0, 10, 9, 12),
            ]},
        )
        report = build_face_transitions(MagicMock(), catalogue)
        assert report.placements_unknown_block == 1
        assert report.transitions_counted == 0

    def test_map_count_aggregates_across_maps(self, catalogue, monkeypatch):
        pl = [("Straight", 0, 10, 9, 10), ("Straight", 0, 10, 9, 11)]
        upserts = _wire_db(
            monkeypatch,
            map_rows=[(1, "Stadium"), (2, "Stadium")],
            placements_by_map={1: list(pl), 2: list(pl)},
        )
        report = build_face_transitions(MagicMock(), catalogue)
        assert report.maps_seen == 2
        assert len(upserts) == 1
        assert upserts[0][6] == 4  # transition_count: 2 per map
        assert upserts[0][7] == 2  # map_count


class TestTransitionKey:
    def test_signature_stable_and_distinct(self):
        k1 = _TransitionKey("A", "B", CLIP, 1, "Stadium")
        k2 = _TransitionKey("A", "B", CLIP, 1, "Stadium")
        k3 = _TransitionKey("B", "A", CLIP, 1, "Stadium")
        assert k1.signature() == k2.signature()
        assert k1.signature() != k3.signature()
