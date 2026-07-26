"""Unit tests for FacePriors and prior-weighted walker ordering."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.generation.priors import UNSEEN_WEIGHT, FacePriors

REAL_CATALOGUE = Path("data/catalogue/catalogue.ndjson")
CLIP = "RoadTechFC"


def _priors_file(tmp_path, rows):
    path = tmp_path / "face_priors.json"
    path.write_text(json.dumps({
        "schema": "face_priors_v1",
        "environment": "Stadium2020",
        "min_maps": 2,
        "priors": rows,
    }), encoding="utf-8")
    return path


class TestFacePriors:
    def test_round_trip_and_weight(self, tmp_path):
        priors = FacePriors.from_json(_priors_file(tmp_path, [
            ["Straight", "Curve", CLIP, 1, 5000, 900],
        ]))
        assert len(priors) == 1
        # rel_rotation = (next - prev) % 4 == 1 must match…
        assert priors.weight("Straight", 0, "Curve", 1, CLIP) == 900 + UNSEEN_WEIGHT
        assert priors.weight("Straight", 3, "Curve", 0, CLIP) == 900 + UNSEEN_WEIGHT
        # …and other rotations / directions / clips fall to smoothing.
        assert priors.weight("Straight", 0, "Curve", 2, CLIP) == UNSEEN_WEIGHT
        assert priors.weight("Curve", 0, "Straight", 1, CLIP) == UNSEEN_WEIGHT
        assert priors.weight("Straight", 0, "Curve", 1, "OtherClip") == UNSEEN_WEIGHT

    def test_wrong_schema_rejected(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"schema": "nope", "priors": []}))
        with pytest.raises(ValueError):
            FacePriors.from_json(path)


class TestPriorWeightedWalker:
    @pytest.fixture()
    def catalogue(self, tmp_path):
        from tests.unit.test_clip_walker import _block
        records = [
            _block("Start", "Start", {"n": [CLIP]}),
            _block("Finish", "Finish", {"s": [CLIP]}),
            _block("Straight", "None", {"n": [CLIP], "s": [CLIP]}),
            _block("Curve", "None", {"n": [CLIP], "e": [CLIP]}),
        ]
        from src.catalogue.loader import load_catalogue
        path = tmp_path / "catalogue.ndjson"
        lines = [json.dumps({"type": "meta", "schema": "block_catalogue_v1"})]
        lines += [json.dumps(r) for r in records]
        path.write_text("\n".join(lines), encoding="utf-8")
        (tmp_path / "catalogue.done.json").write_text("{}", encoding="utf-8")
        return load_catalogue(path)

    def _curve_fraction(self, catalogue, seed, priors):
        from src.generation.clip_walker import ClipWalker
        walker = ClipWalker(
            catalogue, ["Start", "Finish", "Straight", "Curve"],
            seed=seed, priors=priors,
        )
        placements = walker.generate(length=30, checkpoint_every=0)
        plain = [p for p in placements if p.block_id in ("Straight", "Curve")]
        return sum(1 for p in plain if p.block_id == "Curve") / len(plain)

    def test_priors_shift_block_mix(self, catalogue, tmp_path):
        # Corpus "loves curves": any continuation into Curve is 500x
        # a continuation into Straight, at every relative rotation.
        rows = [
            [a, "Curve", CLIP, rel, 9999, 500]
            for a in ("Start", "Straight", "Curve")
            for rel in range(4)
        ]
        curve_priors = FacePriors.from_json(_priors_file(tmp_path, rows))
        shifted = 0
        for seed in (1, 2, 3):
            uniform = self._curve_fraction(catalogue, seed, None)
            weighted = self._curve_fraction(catalogue, seed, curve_priors)
            if weighted > uniform:
                shifted += 1
        assert shifted >= 2, (
            "curve-heavy priors failed to raise curve usage on >=2/3 seeds"
        )

    def test_deterministic_with_priors(self, catalogue, tmp_path):
        from src.generation.clip_walker import ClipWalker
        priors = FacePriors.from_json(_priors_file(tmp_path, [
            ["Straight", "Straight", CLIP, 0, 100, 50],
        ]))
        ids = ["Start", "Finish", "Straight", "Curve"]
        a = ClipWalker(catalogue, ids, seed=9, priors=priors).generate(20, 0)
        b = ClipWalker(catalogue, ids, seed=9, priors=priors).generate(20, 0)
        assert a == b


@pytest.mark.skipif(not REAL_CATALOGUE.is_file(), reason="real catalogue absent")
class TestRealPriorsSmoke:
    REAL_PRIORS = Path("data/catalogue/face_priors.json")

    @pytest.mark.skipif(not REAL_PRIORS.is_file(), reason="real priors absent")
    def test_weighted_route_generates(self):
        from src.catalogue.loader import load_catalogue
        from src.generation.clip_walker import ClipWalker
        from tools.clipwalk_proof import ROADTECH_SET

        catalogue = load_catalogue(REAL_CATALOGUE)
        priors = FacePriors.from_json(self.REAL_PRIORS)
        walker = ClipWalker(catalogue, ROADTECH_SET, seed=7, priors=priors)
        placements = walker.generate(length=60, checkpoint_every=15)
        assert placements[0].block_id == "RoadTechStart"
        assert placements[-1].block_id == "RoadTechFinish"
