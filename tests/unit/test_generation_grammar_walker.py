"""Unit tests for the grammar-driven walker.

The fixture deliberately gives the walker blocks whose clips do NOT
match, because that is the whole point: these routes are legal on
corpus evidence, and ClipWalker cannot build any of them.
"""
from __future__ import annotations

import json

import pytest

from src.catalogue.loader import load_catalogue
from src.generation.clip_walker import RouteDeadEnd
from src.catalogue.loader import rotate_vector
from src.generation.grammar import Move, PlacementGrammar
from src.generation.grammar_walker import (
    GrammarWalker,
    _faces_travel,
    _reverse_offset,
)


def _block(block_id: str, waypoint: str = "None", size=(1, 1, 1)) -> dict:
    units = [
        {"offset": [x, y, z], "underground": False, "terrain_modifier": "",
         "surface": "", "clips": {}}
        for x in range(size[0]) for y in range(size[1]) for z in range(size[2])
    ]
    return {
        "type": "block", "id": block_id, "name": block_id,
        "page": "", "waypoint": waypoint, "is_pillar": False,
        "collection": "Stadium2020",
        "variants": [{
            "kind": "ground", "index": 0, "size": list(size), "units": units,
        }],
    }


@pytest.fixture()
def catalogue(tmp_path):
    records = [
        _block("Start", "Start"),
        _block("Tile"),
        _block("Cp", "Checkpoint"),
        _block("Finish", "Finish"),
        _block("Boost"),          # not directional by name
        _block("SpecialBoost"),   # directional: arrow must face travel
    ]
    path = tmp_path / "catalogue.ndjson"
    lines = [json.dumps({"type": "meta", "schema": "block_catalogue_v1"})]
    lines += [json.dumps(r) for r in records]
    path.write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "catalogue.done.json").write_text("{}", encoding="utf-8")
    return load_catalogue(path, collection="Stadium2020")


def _grammar(tmp_path, rules, offsets=None):
    doc = {
        "schema": "placement_grammar_v1",
        "environment": "Stadium2020",
        "min_maps": 1,
        "offsets": offsets or [[0, 0, 1], [0, 0, 0], [0, 0, 2]],
        "rules": rules,
    }
    path = tmp_path / "grammar.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return PlacementGrammar.from_json(path)


# Every source block continues straight ahead (offset index 0) with
# the same rotation. clip_matched is 0 throughout: nothing here would
# survive the clip model.
def _straight_rules(*blocks, targets=("Tile", "Cp", "Finish")):
    return {
        b: [[t, 0, 0, 500, 500, 0] for t in targets]
        for b in blocks
    }


class TestRouteConstruction:
    def test_builds_a_route_from_clipless_blocks(self, catalogue, tmp_path):
        grammar = _grammar(
            tmp_path, _straight_rules("Start", "Tile", "Cp"))
        walker = GrammarWalker(
            catalogue, grammar,
            pool=["Start", "Tile", "Cp", "Finish"], seed=1, min_maps=1,
        )
        route = walker.generate(length=6, checkpoint_every=0)
        assert route[0].block_id == "Start"
        assert route[-1].block_id == "Finish"
        assert len(route) == 7
        # A straight line north from the origin.
        zs = [p.z for p in route]
        assert zs == sorted(zs) and len(set(zs)) == len(zs)

    def test_route_does_not_overlap_itself(self, catalogue, tmp_path):
        grammar = _grammar(
            tmp_path,
            {b: [[t, o, r, 500, 500, 0]
                 for t in ("Tile", "Finish") for o in (0,) for r in (0, 1, 3)]
             for b in ("Start", "Tile")},
        )
        walker = GrammarWalker(
            catalogue, grammar, pool=["Start", "Tile", "Finish"],
            seed=7, min_maps=1,
        )
        route = walker.generate(length=25, checkpoint_every=0)
        cells = [(p.x, p.y, p.z) for p in route]
        assert len(cells) == len(set(cells))

    def test_checkpoints_are_placed_on_cadence(self, catalogue, tmp_path):
        grammar = _grammar(
            tmp_path, _straight_rules("Start", "Tile", "Cp"))
        walker = GrammarWalker(
            catalogue, grammar, pool=["Start", "Tile", "Cp", "Finish"],
            seed=3, min_maps=1,
        )
        # Steps 3, 6 and 9 are checkpoint slots; step 10 is the finish,
        # which outranks the cadence.
        route = walker.generate(length=10, checkpoint_every=3)
        assert [p.block_id for p in route].count("Cp") == 3

    def test_min_maps_gates_the_vocabulary(self, catalogue, tmp_path):
        grammar = _grammar(
            tmp_path,
            {"Start": [["Tile", 0, 0, 5, 5, 0], ["Finish", 0, 0, 900, 900, 0]],
             "Tile": [["Finish", 0, 0, 900, 900, 0]]},
        )
        walker = GrammarWalker(
            catalogue, grammar, pool=["Start", "Tile", "Finish"],
            seed=1, min_maps=100,
        )
        # Tile is attested in 5 maps, below the bar, so the only route
        # left is Start -> Finish.
        route = walker.generate(length=1, checkpoint_every=0)
        assert [p.block_id for p in route] == ["Start", "Finish"]

    def test_dead_end_is_reported_not_papered_over(self, catalogue, tmp_path):
        grammar = _grammar(tmp_path, {"Start": [["Tile", 0, 0, 500, 500, 0]]})
        walker = GrammarWalker(
            catalogue, grammar, pool=["Start", "Tile", "Finish"],
            seed=1, min_maps=1,
        )
        with pytest.raises(RouteDeadEnd):
            walker.generate(length=4, checkpoint_every=0)

    def test_pool_without_a_finish_is_rejected_up_front(
        self, catalogue, tmp_path
    ):
        grammar = _grammar(tmp_path, _straight_rules("Start"))
        with pytest.raises(ValueError, match="Start and one Finish"):
            GrammarWalker(
                catalogue, grammar, pool=["Start", "Tile"], seed=1,
            )


class TestJumps:
    def test_a_gap_move_is_usable(self, catalogue, tmp_path):
        """Offset 2 with no clip: a jump. The clip model cannot see it."""
        grammar = _grammar(
            tmp_path,
            {"Start": [["Tile", 2, 0, 500, 500, 0]],
             "Tile": [["Finish", 2, 0, 500, 500, 0]]},
        )
        walker = GrammarWalker(
            catalogue, grammar, pool=["Start", "Tile", "Finish"],
            seed=1, min_maps=1, allow_jumps=True, gap_min_maps=1,
            # The vocabulary is grown from ADJACENT moves, and this
            # fixture has nothing but gaps, so it would come back empty.
            route_only=False,
        )
        route = walker.generate(length=2, checkpoint_every=0)
        assert [p.z for p in route] == [route[0].z, route[0].z + 2,
                                        route[0].z + 4]

    def test_jumps_are_off_by_default(self, catalogue, tmp_path):
        grammar = _grammar(
            tmp_path,
            {"Start": [["Tile", 2, 0, 500, 500, 0]],
             "Tile": [["Finish", 2, 0, 500, 500, 0]]},
        )
        walker = GrammarWalker(
            catalogue, grammar, pool=["Start", "Tile", "Finish"],
            seed=1, min_maps=1,
        )
        with pytest.raises(RouteDeadEnd):
            walker.generate(length=2, checkpoint_every=0)

    def test_a_thin_gap_move_is_refused_even_when_jumps_are_on(
        self, catalogue, tmp_path
    ):
        """A real jump and a coincidence look identical bar breadth."""
        grammar = _grammar(
            tmp_path,
            {"Start": [["Tile", 2, 0, 40, 40, 0], ["Finish", 0, 0, 40, 40, 0]],
             "Tile": [["Finish", 2, 0, 40, 40, 0]]},
        )
        walker = GrammarWalker(
            catalogue, grammar, pool=["Start", "Tile", "Finish"],
            seed=1, min_maps=10, allow_jumps=True,  # gap bar becomes 100
            route_only=False,
        )
        # Only the adjacent Start -> Finish move clears the gap bar.
        route = walker.generate(length=1, checkpoint_every=0)
        assert [p.block_id for p in route] == ["Start", "Finish"]


class TestDirectionalBlocks:
    def test_boosters_face_travel(self, catalogue, tmp_path):
        """The bug class that shipped twice: symmetric road, asymmetric arrow."""
        grammar = _grammar(
            tmp_path,
            {"Start": [["SpecialBoost", 0, r, 500, 500, 0] for r in range(4)],
             "SpecialBoost": [["Finish", 0, r, 500, 500, 0] for r in range(4)]},
        )
        for seed in range(6):
            walker = GrammarWalker(
                catalogue, grammar,
                pool=["Start", "SpecialBoost", "Finish"],
                seed=seed, min_maps=1,
            )
            route = walker.generate(length=2, checkpoint_every=0)
            boost = route[1]
            step = (route[2].x - boost.x, 0, route[2].z - boost.z)
            assert _faces_travel(boost.rotation, step), (
                f"seed {seed}: booster at rotation {boost.rotation} "
                f"does not face {step}"
            )


class TestFacesTravel:
    @pytest.mark.parametrize(
        "rotation,travel",
        [(0, (0, 0, 1)), (1, (-1, 0, 0)), (2, (0, 0, -1)), (3, (1, 0, 0))],
    )
    def test_calibrated_frame(self, rotation, travel):
        assert _faces_travel(rotation, travel)
        back = (-travel[0], 0, -travel[2])
        assert not _faces_travel(rotation, back)


class TestReverseOffset:
    """The vector that would undo a move, in the target block's frame.

    Negating the offset is not enough: it lives in the SOURCE frame,
    and the target sits rel_rotation steps away. Get this wrong and a
    U-turn goes unrecognised after any move that also turns.
    """

    @pytest.mark.parametrize("rel", [0, 1, 2, 3])
    @pytest.mark.parametrize("offset", [(0, 0, 1), (1, 0, 0), (2, 1, -1)])
    def test_matches_the_world_space_definition(self, offset, rel):
        move = Move("B", offset, rel, 10, 10, False)
        for source_rot in range(4):
            target_rot = (source_rot + rel) % 4
            world_step = rotate_vector(offset, source_rot)
            world_back = tuple(-v for v in world_step)
            # The same displacement, read in the target's frame.
            expected = rotate_vector(world_back, (4 - target_rot) % 4)
            assert _reverse_offset(move) == expected

    def test_a_straight_move_reverses_to_its_negation(self):
        move = Move("B", (0, 0, 1), 0, 10, 10, False)
        assert _reverse_offset(move) == (0, 0, -1)

    def test_a_turning_move_does_not(self):
        move = Move("B", (0, 0, 1), 1, 10, 10, False)
        assert _reverse_offset(move) != (0, 0, -1)


class TestRouteVocabulary:
    def test_a_block_only_reachable_by_a_jump_stays_out(
        self, catalogue, tmp_path
    ):
        """Landing blocks must be attested on the ground too.

        The vocabulary is grown from ADJACENT moves only, so a block
        the corpus never places next to anything cannot become a jump
        target either. That is deliberate: it is the guard that keeps
        scenery out of routes, and a gap pair is far too weak on its
        own to admit a block.
        """
        grammar = _grammar(
            tmp_path,
            {"Start": [["Finish", 0, 0, 900, 900, 0],
                       ["Tile", 2, 0, 900, 900, 0]]},
        )
        walker = GrammarWalker(
            catalogue, grammar, pool=["Start", "Tile", "Finish"],
            seed=1, min_maps=1, allow_jumps=True, gap_min_maps=1,
        )
        route = walker.generate(length=1, checkpoint_every=0)
        assert [p.block_id for p in route] == ["Start", "Finish"]

    def test_vocabulary_narrows_the_pool(self, catalogue, tmp_path):
        grammar = _grammar(
            tmp_path,
            {"Start": [["Tile", 0, 0, 900, 900, 0]],
             "Tile": [["Finish", 0, 0, 900, 900, 0]]},
        )
        walker = GrammarWalker(
            catalogue, grammar,
            pool=["Start", "Tile", "Cp", "Finish", "Boost"],
            seed=1, min_maps=1,
        )
        # Boost is in the pool but the corpus never places it next to
        # anything on the line, so it is not buildable.
        assert "Boost" not in walker._allow
        assert {"Start", "Tile", "Finish"} <= walker._allow
