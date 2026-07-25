"""Unit tests for src.generation.clip_walker.

The synthetic catalogue mirrors the real RoadTech clip topology
(start exits north-only, straight is n/s, curve is n/e, finish
enters south-only) so walker invariants are exercised without the
15 MB real catalogue. A smoke test against the real catalogue runs
when data/catalogue/catalogue.ndjson exists.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.catalogue.loader import FACE_DELTAS, load_catalogue, opposite_face
from src.generation.clip_walker import ClipWalker, Placement, RouteDeadEnd

REAL_CATALOGUE = Path("data/catalogue/catalogue.ndjson")

CLIP = "RoadTechFC"


def _block(block_id: str, waypoint: str, clips: dict[str, list[str]]) -> dict:
    return {
        "type": "block",
        "id": block_id,
        "name": block_id,
        "page": "RoadTech/Main/",
        "waypoint": waypoint,
        "is_pillar": False,
        "variants": [
            {
                "kind": "ground",
                "index": 0,
                "size": [1, 1, 1],
                "units": [
                    {
                        "offset": [0, 0, 0],
                        "underground": False,
                        "terrain_modifier": "",
                        "surface": "",
                        "clips": clips,
                    }
                ],
            }
        ],
    }


@pytest.fixture()
def synthetic_catalogue(tmp_path):
    records = [
        _block("Start", "Start", {"n": [CLIP]}),
        _block("Finish", "Finish", {"s": [CLIP]}),
        _block("Checkpoint", "Checkpoint", {"n": [CLIP], "s": [CLIP]}),
        _block("Straight", "None", {"n": [CLIP], "s": [CLIP]}),
        _block("Curve", "None", {"n": [CLIP], "e": [CLIP]}),
    ]
    path = tmp_path / "catalogue.ndjson"
    lines = [json.dumps({"type": "meta", "schema": "block_catalogue_v1"})]
    lines += [json.dumps(r) for r in records]
    path.write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "catalogue.done.json").write_text("{}", encoding="utf-8")
    return load_catalogue(path)


def _assert_route_closed(placements: list[Placement]) -> None:
    """Consecutive placements must be grid-adjacent and non-overlapping."""
    cells = [(p.x, p.z) for p in placements]
    assert len(set(cells)) == len(cells), "route overlaps itself"
    for a, b in zip(placements, placements[1:]):
        deltas = {(d[0], d[2]) for d in FACE_DELTAS.values()}
        assert (b.x - a.x, b.z - a.z) in deltas, (
            f"non-adjacent consecutive placements: {a} -> {b}"
        )


class TestClipWalker:
    def test_route_closes_start_to_finish(self, synthetic_catalogue):
        walker = ClipWalker(
            synthetic_catalogue,
            ["Start", "Finish", "Checkpoint", "Straight", "Curve"],
            seed=42,
        )
        placements = walker.generate(length=20, checkpoint_every=8)
        assert placements[0].block_id == "Start"
        assert placements[-1].block_id == "Finish"
        assert any(p.block_id == "Checkpoint" for p in placements)
        assert len(placements) >= 20
        _assert_route_closed(placements)

    def test_deterministic_for_seed(self, synthetic_catalogue):
        ids = ["Start", "Finish", "Straight", "Curve"]
        a = ClipWalker(synthetic_catalogue, ids, seed=7).generate(15)
        b = ClipWalker(synthetic_catalogue, ids, seed=7).generate(15)
        assert a == b

    def test_different_seeds_diverge(self, synthetic_catalogue):
        ids = ["Start", "Finish", "Straight", "Curve"]
        a = ClipWalker(synthetic_catalogue, ids, seed=1).generate(15)
        b = ClipWalker(synthetic_catalogue, ids, seed=2).generate(15)
        assert a != b

    def test_missing_start_rejected(self, synthetic_catalogue):
        with pytest.raises(ValueError):
            ClipWalker(synthetic_catalogue, ["Straight", "Curve"], seed=1)

    def test_impossible_length_raises(self, synthetic_catalogue):
        walker = ClipWalker(
            synthetic_catalogue, ["Start", "Finish", "Straight"], seed=3
        )
        # Straight-only cannot exceed the grid span; the walker must
        # fail loudly, not return a broken route.
        with pytest.raises(RouteDeadEnd):
            walker.generate(length=100, checkpoint_every=0)


@pytest.mark.skipif(not REAL_CATALOGUE.is_file(), reason="real catalogue absent")
class TestRealCatalogueSmoke:
    def test_roadtech_route(self):
        catalogue = load_catalogue(REAL_CATALOGUE)
        walker = ClipWalker(
            catalogue,
            [
                "RoadTechStart",
                "RoadTechFinish",
                "RoadTechCheckpoint",
                "RoadTechStraight",
                "RoadTechCurve1",
            ],
            seed=42,
        )
        placements = walker.generate(length=30, checkpoint_every=10)
        assert placements[0].block_id == "RoadTechStart"
        assert placements[-1].block_id == "RoadTechFinish"
        _assert_route_closed(placements)
