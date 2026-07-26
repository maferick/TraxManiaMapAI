"""Unit tests for MapSpec and the keyword description translator."""
from __future__ import annotations

import json

import pytest

from src.generation.families import FamilyError
from src.generation.spec import (
    SCHEMA,
    MapSpec,
    SpecError,
    from_description,
)


class TestMapSpecValidation:
    def test_defaults_are_valid(self):
        MapSpec().validate()

    def test_unknown_family_rejected(self):
        with pytest.raises(SpecError, match="unknown family"):
            MapSpec(family="lava").validate()

    def test_unsupported_family_explains_itself(self):
        with pytest.raises(FamilyError, match="isolated clip"):
            MapSpec(family="platform-tech").validate()

    def test_absurd_length_rejected(self):
        with pytest.raises(SpecError, match="too short"):
            MapSpec(length=1).validate()

    def test_negative_bias_rejected(self):
        with pytest.raises(SpecError, match="negative"):
            MapSpec(bias={"Curve1": -1.0}).validate()


class TestJsonRoundTrip:
    def test_round_trip(self):
        spec = MapSpec(family="dirt", length=42, bias={"Curve2": 3.0},
                       description="flowy dirt")
        restored = MapSpec.from_json(spec.to_json())
        assert restored == spec

    def test_schema_is_stamped(self):
        assert json.loads(MapSpec().to_json())["schema"] == SCHEMA

    def test_bad_schema_rejected(self):
        with pytest.raises(SpecError, match="unsupported spec schema"):
            MapSpec.from_json(json.dumps({"schema": "map_spec_v99"}))


class TestFromDescription:
    def test_family_from_word(self):
        assert from_description("a dirt map").family == "dirt"
        assert from_description("icy track").family == "ice"
        assert from_description("bumpy thing").family == "bump"

    def test_family_defaults_to_tech(self):
        assert from_description("something flowy").family == "tech"

    def test_length_word_and_explicit_count(self):
        assert from_description("a short map").length == 30
        # An explicit count overrides the word.
        assert from_description("a short map, 77 blocks").length == 77

    def test_checkpoint_count_becomes_cadence(self):
        spec = from_description("tech map, 60 blocks, 3 checkpoints")
        assert spec.checkpoint_every == 15  # 60 // (3+1)

    def test_style_bias_applied(self):
        bias = from_description("flowy map").bias
        assert bias["Curve2"] > 1.0
        assert bias["Curve1"] < 1.0

    def test_styles_compose_multiplicatively(self):
        one = from_description("twisty map").bias["Curve1"]
        both = from_description("twisty tight map").bias["Curve1"]
        assert both > one

    def test_ban_beats_preference(self):
        # "clean" bans penalty blocks; "nasty" wants them. A ban wins
        # regardless of word order, so the result is predictable.
        for text in ("clean nasty map", "nasty clean map"):
            bias = from_description(text).bias
            assert bias.get("PenaltyIce") == 0.0 or bias.get("Penalty") == 0.0

    def test_flat_bans_slopes(self):
        assert from_description("flat map").bias["Slope"] == 0.0

    def test_description_recorded_for_provenance(self):
        spec = from_description("  flowy dirt  ")
        assert spec.description == "flowy dirt"

    def test_seed_passthrough(self):
        assert from_description("tech", seed=7).seed == 7

    def test_unknown_words_are_ignored_not_fatal(self):
        spec = from_description("a wonderful serpentine flowy dirt map")
        assert spec.family == "dirt"
        assert spec.bias["Curve2"] > 1.0

    def test_result_is_always_valid(self):
        for text in ("", "flowy", "nasty icy huge 200 blocks 9 cps",
                     "clean flat slow water"):
            from_description(text).validate()


class TestDeterminism:
    def test_same_text_same_spec(self):
        a = from_description("flowy dirt, 40 blocks", seed=3)
        b = from_description("flowy dirt, 40 blocks", seed=3)
        assert a == b
