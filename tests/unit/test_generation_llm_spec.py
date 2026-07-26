"""Unit tests for the LLM spec translator.

No network: the Ollama call is monkeypatched. What matters here is the
guardrails — a model will happily invent block names, oversized
lengths and unsupported families, and none of that may reach the
generator.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from src.generation import llm_spec
from src.generation.llm_spec import (
    MAX_BIAS,
    MAX_LENGTH,
    MIN_LENGTH,
    from_description_llm,
    known_bias_keys,
)


def _reply(monkeypatch, payload):
    """Make the LLM 'return' payload (dict -> JSON, or a raw string)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(
        llm_spec, "_ollama_chat",
        lambda prompt, host, model, timeout: text,
    )


def _boom(monkeypatch, exc):
    def raise_it(prompt, host, model, timeout):
        raise exc
    monkeypatch.setattr(llm_spec, "_ollama_chat", raise_it)


class TestKnownBiasKeys:
    def test_includes_vocabulary_keys(self):
        keys = known_bias_keys(None)
        assert "Curve1" in keys and "Chicane" in keys and "Slope" in keys


class TestHappyPath:
    def test_uses_model_output(self, monkeypatch):
        _reply(monkeypatch, {
            "family": "dirt", "length": 45, "checkpoint_every": 9,
            "bias": {"Curve2": 2.0, "Chicane": 0.5},
        })
        spec = from_description_llm("muddy stage", seed=4,
                                    catalogue_path=None)
        assert (spec.family, spec.length, spec.checkpoint_every) == ("dirt", 45, 9)
        assert spec.bias == {"Curve2": 2.0, "Chicane": 0.5}
        assert spec.seed == 4
        assert spec.description == "muddy stage"


class TestGuardrails:
    def test_unsupported_family_ignored(self, monkeypatch):
        # Platform families exist but cannot be walked; a model
        # suggesting one must not break generation.
        _reply(monkeypatch, {"family": "platform-tech", "bias": {}})
        assert from_description_llm("x", catalogue_path=None).family == "tech"

    def test_invented_family_ignored(self, monkeypatch):
        _reply(monkeypatch, {"family": "lava", "bias": {}})
        assert from_description_llm("x", catalogue_path=None).family == "tech"

    def test_invented_bias_keys_dropped(self, monkeypatch):
        _reply(monkeypatch, {
            "bias": {"Curve1": 2.0, "RocketLauncher": 5.0, "Nitro": 3.0},
        })
        spec = from_description_llm("x", catalogue_path=None)
        assert spec.bias == {"Curve1": 2.0}

    def test_length_clamped(self, monkeypatch):
        _reply(monkeypatch, {"length": 999999, "bias": {}})
        assert from_description_llm("x", catalogue_path=None).length == MAX_LENGTH
        _reply(monkeypatch, {"length": 1, "bias": {}})
        assert from_description_llm("x", catalogue_path=None).length == MIN_LENGTH

    def test_bias_weight_clamped_and_non_negative(self, monkeypatch):
        _reply(monkeypatch, {"bias": {"Curve1": 500.0, "Chicane": -3.0}})
        spec = from_description_llm("x", catalogue_path=None)
        assert spec.bias["Curve1"] == MAX_BIAS
        assert spec.bias["Chicane"] == 0.0

    def test_checkpoint_cadence_cannot_exceed_length(self, monkeypatch):
        _reply(monkeypatch, {"length": 30, "checkpoint_every": 900, "bias": {}})
        spec = from_description_llm("x", catalogue_path=None)
        assert spec.checkpoint_every <= spec.length

    def test_non_numeric_fields_tolerated(self, monkeypatch):
        _reply(monkeypatch, {"length": "quite long", "checkpoint_every": None,
                             "bias": {"Curve1": "lots"}})
        spec = from_description_llm("x", catalogue_path=None)
        spec.validate()
        assert "Curve1" not in spec.bias

    def test_output_always_validates(self, monkeypatch):
        _reply(monkeypatch, {"family": "ice", "length": 40,
                             "checkpoint_every": 8, "bias": {"Curve1": 3.0}})
        from_description_llm("x", catalogue_path=None).validate()


class TestFallback:
    def test_host_down_falls_back_to_keywords(self, monkeypatch):
        _boom(monkeypatch, urllib.error.URLError("refused"))
        spec = from_description_llm("flowy dirt map", catalogue_path=None)
        # Keyword translator result, so generation still works.
        assert spec.family == "dirt"
        assert spec.bias["Curve2"] > 1.0

    def test_garbage_json_falls_back(self, monkeypatch):
        _reply(monkeypatch, "not json at all")
        assert from_description_llm("icy map", catalogue_path=None).family == "ice"

    def test_json_array_falls_back(self, monkeypatch):
        _reply(monkeypatch, "[1, 2, 3]")
        assert from_description_llm("icy map", catalogue_path=None).family == "ice"

    def test_fallback_can_be_disabled(self, monkeypatch):
        _boom(monkeypatch, urllib.error.URLError("refused"))
        with pytest.raises(RuntimeError, match="LLM translation failed"):
            from_description_llm("x", catalogue_path=None, fallback=False)


class TestRetry:
    def test_retries_once_on_server_error(self, monkeypatch):
        calls = {"n": 0}

        def flaky(url, timeout):  # urlopen signature
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    "u", 500, "warming", {}, None)  # type: ignore[arg-type]

            class R:
                def read(self):
                    return json.dumps(
                        {"message": {"content": json.dumps({"family": "ice"})}}
                    ).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return R()

        monkeypatch.setattr(llm_spec.urllib.request, "urlopen", flaky)
        spec = from_description_llm("x", catalogue_path=None)
        assert calls["n"] == 2, "should retry a cold-model 500 exactly once"
        assert spec.family == "ice"
