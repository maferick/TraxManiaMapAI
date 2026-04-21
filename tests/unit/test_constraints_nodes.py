from __future__ import annotations

import pytest

from src.constraints.nodes import (
    AdjacencyObservation,
    BlockKey,
    order_pair,
)


def test_normalized_key_joins_with_separator() -> None:
    k = BlockKey(family="tech", type="straight", variant="short")
    assert k.normalized_key == "tech|straight|short"


def test_variant_defaults_to_empty_string() -> None:
    k = BlockKey(family="tech", type="straight")
    assert k.variant == ""
    assert k.normalized_key == "tech|straight|"


def test_rejects_empty_family() -> None:
    with pytest.raises(ValueError, match="family"):
        BlockKey(family="", type="straight")


def test_rejects_empty_type() -> None:
    with pytest.raises(ValueError, match="type"):
        BlockKey(family="tech", type="")


def test_rejects_separator_in_components() -> None:
    with pytest.raises(ValueError, match="separator"):
        BlockKey(family="te|ch", type="straight")
    with pytest.raises(ValueError, match="separator"):
        BlockKey(family="tech", type="straight", variant="a|b")


def test_from_normalized_round_trip() -> None:
    k = BlockKey(family="dirt", type="curve", variant="r2")
    assert BlockKey.from_normalized(k.normalized_key) == k


def test_from_normalized_rejects_wrong_part_count() -> None:
    with pytest.raises(ValueError, match="3 parts"):
        BlockKey.from_normalized("tech|straight")


def test_order_pair_is_deterministic() -> None:
    a = BlockKey(family="a", type="x")
    b = BlockKey(family="b", type="x")
    assert order_pair(a, b) == (a, b)
    assert order_pair(b, a) == (a, b)


def test_adjacency_observation_requires_ordered_pair() -> None:
    a = BlockKey(family="a", type="x")
    b = BlockKey(family="b", type="x")
    # Wrong order raises.
    with pytest.raises(ValueError, match="lexicographic"):
        AdjacencyObservation(a=b, b=a, snapshot_id="s1")
    # Right order is fine.
    obs = AdjacencyObservation(a=a, b=b, snapshot_id="s1")
    assert obs.snapshot_id == "s1"
    assert obs.is_benchmark_strong is False
    assert obs.is_broken_fixture is False
