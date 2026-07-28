"""Round-trip guarantees for the LM text serialization.

If serialize/parse do not invert each other exactly, every sampled
sequence is unevaluable and every quality number downstream is fiction,
so this is tested over the real corpus, not just toy cases.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.learning.construction_text import ParseError, parse, serialize

CORPUS = Path("data/artifacts/telemetry/construction_sequences_v0.2.jsonl")


def test_round_trip_simple():
    tokens = [
        {"op": "PLACE", "block": "RoadTechStraight", "d": [0, 0, 0],
         "rot": 2, "free": False},
        {"op": "JUMP"},
        {"op": "PLACE", "block": "PlatformDirtBase", "d": [1, -1, 3],
         "rot": 0, "free": True},
        {"op": "REVISIT", "back": 2},
        {"op": "GAP"},
    ]
    assert parse(serialize(tokens)) == tokens


def test_space_in_custom_block_name_survives():
    tokens = [{"op": "PLACE",
               "block": "WoodPlatform (Fixed)\\PlatformWoodBase.Block.Gbx_CustomBlock",
               "d": [0, 0, 1], "rot": 1, "free": True}]
    assert parse(serialize(tokens)) == tokens


def test_garbage_raises_not_repairs():
    with pytest.raises(ParseError):
        parse("#len=med P RoadTechStraight 0 0")     # truncated
    with pytest.raises(ParseError):
        parse("P Road 0 0 0 7")                       # rotation range
    with pytest.raises(ParseError):
        parse("XYZZY 1 2 3")                          # unknown op


@pytest.mark.skipif(not CORPUS.is_file(), reason="corpus not present")
def test_whole_corpus_round_trips():
    checked = 0
    for line in CORPUS.open(encoding="utf-8"):
        doc = json.loads(line)
        tokens = doc["tokens"]
        # Normalise shape: exporter emits exactly these keys.
        assert parse(serialize(tokens)) == tokens
        checked += 1
    assert checked == 1231
