"""Unit tests for surface-family resolution."""
from __future__ import annotations

import json

import pytest

from src.catalogue.loader import load_catalogue
from src.generation.families import (
    FAMILIES,
    SUPPORTED,
    FamilyError,
    resolve,
    resolve_pool,
)

REAL_CATALOGUE = "data/catalogue2/catalogue.ndjson"


def _block(block_id, waypoint, clip, size=(1, 1, 1)):
    # clip=None reproduces the Gate* arches, which carry no clips at
    # all — invisible to resolve(), usable by resolve_pool().
    clips = {} if clip is None else {"n": [clip], "s": [clip]}
    return {
        "type": "block", "id": block_id, "name": block_id, "page": "p",
        "waypoint": waypoint, "is_pillar": False,
        "variants": [{
            "kind": "ground", "index": 0, "size": list(size),
            "units": [{"offset": [0, 0, 0], "underground": False,
                       "terrain_modifier": "", "surface": "",
                       "clips": clips}],
        }],
    }


@pytest.fixture()
def catalogue(tmp_path):
    records = [
        _block("RoadTechStart", "Start", "RoadTechFC"),
        _block("RoadTechFinish", "Finish", "RoadTechFC"),
        _block("RoadTechStraight", "None", "RoadTechFC"),
        _block("RoadTechWide", "None", "RoadTechFC", size=(4, 1, 4)),
        _block("RoadDirtStart", "Start", "RoadDirtFC"),
        _block("RoadDirtFinish", "Finish", "RoadDirtFC"),
        _block("RoadDirtStraight", "None", "RoadDirtFC"),
        # Wrong clip for its family — must be filtered out.
        _block("RoadTechStray", "None", "SomethingElseFC"),
        # Platform surface and its gate, with the mismatched clips the
        # real catalogue has: the gate carries PlatformFCSmallRacing,
        # which no surface block does.
        _block("PlatformPlasticBase", "None", "PlatFormFCSmall"),
        _block("PlatformPlasticStart", "Start", "PlatformFCSmallRacing"),
        _block("PlatformPlasticFinish", "Finish", "PlatformFCSmallRacing"),
        # Universal arches: no clips whatsoever.
        _block("GateCheckpoint", "Checkpoint", None, size=(1, 4, 1)),
        _block("GateFinish", "Finish", None, size=(1, 4, 1)),
    ]
    path = tmp_path / "catalogue.ndjson"
    lines = [json.dumps({"type": "meta", "schema": "block_catalogue_v1"})]
    lines += [json.dumps(r) for r in records]
    path.write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "catalogue.done.json").write_text("{}", encoding="utf-8")
    return load_catalogue(path)


class TestResolve:
    def test_single_family(self, catalogue):
        ids, clips = resolve(["tech"], catalogue)
        assert clips == frozenset({"RoadTechFC"})
        assert "RoadTechStraight" in ids
        assert "RoadDirtStraight" not in ids

    def test_blocks_without_the_family_clip_are_dropped(self, catalogue):
        ids, _ = resolve(["tech"], catalogue)
        assert "RoadTechStray" not in ids

    def test_max_footprint_filter(self, catalogue):
        ids, _ = resolve(["tech"], catalogue, max_footprint=3)
        assert "RoadTechWide" not in ids
        assert "RoadTechStraight" in ids

    def test_mixing_incompatible_clips_rejected(self, catalogue):
        # tech + dirt would give a pool whose halves can never
        # connect; fail loudly rather than as a mystery dead end.
        with pytest.raises(FamilyError, match="cannot interconnect"):
            resolve(["tech", "dirt"], catalogue)

    def test_platform_rejected_with_reason(self, catalogue):
        with pytest.raises(FamilyError, match="isolated clip"):
            resolve(["platform-tech"], catalogue)

    def test_unknown_family(self, catalogue):
        with pytest.raises(FamilyError, match="unknown families"):
            resolve(["lava"], catalogue)

    def test_missing_gates_rejected(self, catalogue):
        with pytest.raises(FamilyError, match="Start"):
            resolve(["bump"], catalogue)  # none in this fixture

    def test_empty_request(self, catalogue):
        with pytest.raises(FamilyError):
            resolve([], catalogue)


class TestFamilyTable:
    def test_supported_are_the_road_families(self):
        assert set(SUPPORTED) == {"tech", "dirt", "bump", "ice", "water"}

    def test_every_platform_family_carries_a_reason(self):
        for name, fam in FAMILIES.items():
            if name.startswith("platform-"):
                assert fam.unsupported, f"{name} must explain why it is out"


@pytest.mark.skipif(
    not __import__("pathlib").Path(REAL_CATALOGUE).is_file(),
    reason="real catalogue absent",
)
class TestRealCatalogue:
    def test_every_supported_family_builds_a_route(self):
        from src.generation.clip_walker import ClipWalker

        catalogue = load_catalogue(REAL_CATALOGUE, collection="Stadium2020")
        for family in SUPPORTED:
            ids, clips = resolve([family], catalogue, max_footprint=3)
            route = ClipWalker(
                catalogue, ids, seed=5, route_clips=clips
            ).generate(30, 10)
            assert route[0].block_id.endswith("Start"), family
            assert route[-1].block_id.endswith("Finish"), family


class TestResolvePool:
    """The grammar walker's vocabulary: no clip rule, so no clip limits."""

    def test_platform_families_resolve(self, catalogue):
        # resolve() refuses these because platform gates expose an
        # isolated clip. Corpus map 25192 is a published plastic map
        # that places them anyway.
        pool = resolve_pool(["platform-plastic"], catalogue)
        assert any(catalogue[b].waypoint == "Start" for b in pool)
        assert any(catalogue[b].waypoint == "Finish" for b in pool)

    def test_families_may_mix(self, catalogue):
        mixed = resolve_pool(["dirt", "platform-plastic"], catalogue)
        assert set(mixed) >= set(resolve_pool(["dirt"], catalogue))
        assert set(mixed) >= set(resolve_pool(["platform-plastic"], catalogue))

    def test_clipless_gates_are_included(self, catalogue):
        pool = resolve_pool(["dirt"], catalogue, gates=True)
        gates = [b for b in pool if b.startswith("Gate")]
        assert gates, "universal Gate* arches must reach the pool"
        # The whole point: these have no clips, so resolve() drops them.
        clipless = [
            b for b in gates
            if not catalogue[b].variant("ground", 0).side_ports()
        ]
        assert clipless

    def test_gates_can_be_switched_off(self, catalogue):
        pool = resolve_pool(["dirt"], catalogue, gates=False)
        assert not [b for b in pool if b.startswith("Gate")]

    def test_unknown_family_is_rejected(self, catalogue):
        with pytest.raises(FamilyError, match="unknown families"):
            resolve_pool(["banana"], catalogue)
