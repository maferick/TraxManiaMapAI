from __future__ import annotations

from src.constraints.extractor import extract_adjacencies, unique_block_keys
from src.constraints.nodes import BlockKey
from src.schema.maps import BlockPlacement


def _placement(
    x: int,
    y: int,
    z: int,
    *,
    family: str = "tech",
    type: str = "straight",
    variant: str | None = None,
    idx: int = 0,
) -> BlockPlacement:
    return BlockPlacement(
        id=None,
        map_id=1,
        parser_version="0.0.0",
        created_by_version="0.1.0",
        source_artifact_ids={},
        block_family=family,
        block_type=type,
        placement_index=idx,
        x=x,
        y=y,
        z=z,
        variant=variant,
    )


def test_two_axis_neighbors_produce_one_observation() -> None:
    placements = [
        _placement(0, 0, 0, idx=0),
        _placement(1, 0, 0, type="curve", idx=1),
    ]
    obs = extract_adjacencies(placements, snapshot_id="s1")
    assert len(obs) == 1
    o = obs[0]
    assert o.a.normalized_key < o.b.normalized_key
    assert o.snapshot_id == "s1"
    assert o.is_benchmark_strong is False


def test_diagonal_neighbors_are_ignored() -> None:
    placements = [
        _placement(0, 0, 0, idx=0),
        _placement(1, 1, 0, type="curve", idx=1),  # diagonal
    ]
    obs = extract_adjacencies(placements, snapshot_id="s1")
    assert obs == []


def test_dedup_same_pair_from_different_touches() -> None:
    # Three blocks of type 'straight' in a row along x. Three unordered
    # pairs: A-B, B-C. But A-C aren't adjacent. So we get 2 observations.
    # However, A and B share the same block type; if A and C were the
    # same type (they are), and B is between them, the (straight,straight)
    # pair is observed twice (A-B and B-C). Dedup keeps one.
    placements = [
        _placement(0, 0, 0, idx=0),
        _placement(1, 0, 0, idx=1),
        _placement(2, 0, 0, idx=2),
    ]
    obs = extract_adjacencies(placements, snapshot_id="s1")
    assert len(obs) == 1  # (straight,straight) deduped
    o = obs[0]
    assert o.a == o.b  # same block type; self-adjacency observation


def test_multiple_pair_types_preserved() -> None:
    placements = [
        _placement(0, 0, 0, type="straight", idx=0),
        _placement(1, 0, 0, type="curve", idx=1),
        _placement(0, 1, 0, type="ramp", idx=2),
        _placement(0, 0, 1, type="platform", idx=3),
    ]
    obs = extract_adjacencies(placements, snapshot_id="s1")
    # All three neighbors adjacent to (0,0,0): (straight,curve),
    # (straight,ramp), (straight,platform). No other pairs.
    pairs = {(o.a.normalized_key, o.b.normalized_key) for o in obs}
    assert len(pairs) == 3


def test_benchmark_strong_flag_propagates() -> None:
    placements = [
        _placement(0, 0, 0, idx=0),
        _placement(1, 0, 0, type="curve", idx=1),
    ]
    obs = extract_adjacencies(
        placements, snapshot_id="s1", is_benchmark_strong=True
    )
    assert len(obs) == 1
    assert obs[0].is_benchmark_strong is True


def test_broken_fixture_flag_propagates() -> None:
    placements = [
        _placement(0, 0, 0, idx=0),
        _placement(1, 0, 0, type="curve", idx=1),
    ]
    obs = extract_adjacencies(
        placements, snapshot_id="s1", is_broken_fixture=True
    )
    assert obs[0].is_broken_fixture is True


def test_first_block_wins_for_conflicting_cell() -> None:
    # If two placements share a cell, the first (by list order) wins.
    # This is scaffold policy — real TM2020 data shouldn't produce
    # collisions.
    placements = [
        _placement(0, 0, 0, type="straight", idx=0),
        _placement(0, 0, 0, type="ramp", idx=1),  # same cell, ignored
        _placement(1, 0, 0, type="curve", idx=2),
    ]
    obs = extract_adjacencies(placements, snapshot_id="s1")
    # Only (straight,curve), not (ramp,curve).
    assert len(obs) == 1
    assert {"tech|straight|", "tech|curve|"} == {
        obs[0].a.normalized_key,
        obs[0].b.normalized_key,
    }


def test_unique_block_keys_helper() -> None:
    obs = extract_adjacencies(
        [
            _placement(0, 0, 0, type="a", idx=0),
            _placement(1, 0, 0, type="b", idx=1),
            _placement(2, 0, 0, type="c", idx=2),
        ],
        snapshot_id="s1",
    )
    keys = unique_block_keys(obs)
    key_strs = {k.normalized_key for k in keys}
    assert key_strs == {"tech|a|", "tech|b|", "tech|c|"}


def test_empty_placements_returns_empty() -> None:
    assert extract_adjacencies([], snapshot_id="s1") == []


def test_variant_affects_identity() -> None:
    placements = [
        _placement(0, 0, 0, type="ramp", variant="short", idx=0),
        _placement(1, 0, 0, type="ramp", variant="long", idx=1),
    ]
    obs = extract_adjacencies(placements, snapshot_id="s1")
    assert len(obs) == 1
    # Two distinct block identities because of variant.
    assert obs[0].a != obs[0].b
