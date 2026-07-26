"""LLM translator: free text -> MapSpec.

This replaces exactly one function — :func:`spec.from_description` —
and emits the same :class:`MapSpec`. Nothing downstream changes, and
generation stays headless and deterministic: the model chooses the
spec, then a spec plus a seed always produces the same map.

Why an LLM at all, when the keyword translator already works: it
handles phrasing the vocabulary does not literally contain ("a rally
stage in the rain", "something brutal for a time attack") and can
infer length or checkpoint count from intent rather than keywords.

Guardrails, because a model will happily invent block names:

* ``family`` must be one of the supported families.
* every ``bias`` key must be a substring that really occurs in
  Stadium2020 block ids — validated against the catalogue, unknown
  keys are dropped with a warning rather than passed through.
* numeric fields are clamped to sane ranges.
* any failure (host down, bad JSON, empty result) falls back to the
  keyword translator, so this is strictly additive.

Backend is a local Ollama by default: no API key, nothing leaves the
network.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from src.generation.families import SUPPORTED
from src.generation.spec import (
    LENGTH_WORDS,
    STYLE_VOCAB,
    MapSpec,
    from_description,
)

_LOG = logging.getLogger(__name__)

# Local GPU Ollama by default (RTX 4070 Ti SUPER, 16 GB VRAM). Keeps
# translation on-machine and fast; no API key, nothing leaves the host.
# Override with TM_LLM_HOST / TM_LLM_MODEL for the LAN boxes (the
# Ollama LXC at 192.168.178.56 is CPU-only and much slower).
DEFAULT_HOST = os.environ.get("TM_LLM_HOST", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("TM_LLM_MODEL", "gpt-oss:20b")
DEFAULT_TIMEOUT = int(os.environ.get("TM_LLM_TIMEOUT", "180"))

# Sane bounds; a model asked for "a huge map" will happily say 100000.
MIN_LENGTH, MAX_LENGTH = 10, 400
MAX_BIAS = 20.0


def known_bias_keys(catalogue_path: str | Path | None = None) -> set[str]:
    """Substrings a bias key may use.

    The vocabulary's own keys, plus — when a catalogue is available —
    every CamelCase token appearing in a Stadium2020 block id. That
    keeps the model from biasing on names that do not exist.
    """
    keys: set[str] = set()
    for mapping in STYLE_VOCAB.values():
        keys.update(mapping)
    if catalogue_path and Path(catalogue_path).is_file():
        with open(catalogue_path, encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                if rec.get("type") != "block":
                    continue
                if rec.get("collection") != "Stadium2020":
                    continue
                keys.update(re.findall(r"[A-Z][a-z0-9]+", rec["id"]))
    return keys


def _prompt(text: str) -> str:
    vocab = sorted({k for m in STYLE_VOCAB.values() for k in m})
    return (
        "You translate a Trackmania map request into JSON. "
        "Reply with ONLY a JSON object, no prose.\n\n"
        "Fields:\n"
        f'  "family": one of {SUPPORTED} (surface type)\n'
        '  "length": integer, number of track blocks (short~30, '
        'medium~60, long~100)\n'
        '  "checkpoint_every": integer, blocks between checkpoints '
        "(0 = none)\n"
        '  "bias": object mapping a BLOCK-NAME SUBSTRING to a weight.\n'
        "      weight > 1 prefers those blocks, < 1 avoids them, "
        "0 bans them.\n"
        f"      Use only these substrings: {vocab}\n"
        "      Curve1 is a tight corner, Curve2/Curve3 are wide "
        "sweeping ones.\n"
        "      Chicane is a quick left-right. Slope changes height.\n"
        "      SpecialBoost/SpecialTurbo speed the car up.\n"
        "      Penalty*/SpecialFragile are hazards.\n\n"
        "Interpret the intent, not just the words. Examples:\n"
        '  "a rally stage in the mud" -> family dirt, some Slope, '
        "wide curves\n"
        '  "brutal time attack" -> tight Curve1, Chicane, hazards\n\n'
        f"Request: {text}\n"
    )


def _ollama_chat(prompt: str, host: str, model: str, timeout: int) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        # Deterministic-ish: same request should give the same spec.
        "options": {"temperature": 0.2},
    }
    data = json.dumps(payload).encode()
    last: Exception | None = None
    # A cold model can 500 while it loads (~30 s for a 20B on GPU).
    # That is transient, so retry once before giving up and falling
    # back to keywords.
    for attempt in (1, 2):
        req = urllib.request.Request(
            f"{host.rstrip('/')}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
            return body.get("message", {}).get("content", "")
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code >= 500 and attempt == 1:
                _LOG.info("LLM returned %d (model warming); retrying", exc.code)
                continue
            raise
    raise last if last else RuntimeError("unreachable")


def _coerce(raw: dict, text: str, seed: int, bias_keys: set[str]) -> MapSpec:
    spec = MapSpec(seed=seed, description=text.strip())

    family = str(raw.get("family", "")).strip().lower()
    if family in SUPPORTED:
        spec.family = family
    elif family:
        _LOG.warning("model proposed unsupported family %r; keeping %r",
                     family, spec.family)

    try:
        length = int(raw.get("length", spec.length))
        spec.length = max(MIN_LENGTH, min(MAX_LENGTH, length))
    except (TypeError, ValueError):
        pass

    try:
        cp = int(raw.get("checkpoint_every", spec.checkpoint_every))
        spec.checkpoint_every = max(0, min(spec.length, cp))
    except (TypeError, ValueError):
        pass

    bias: dict[str, float] = {}
    dropped: list[str] = []
    for key, value in (raw.get("bias") or {}).items():
        key = str(key)
        if bias_keys and key not in bias_keys:
            dropped.append(key)
            continue
        try:
            weight = float(value)
        except (TypeError, ValueError):
            dropped.append(key)
            continue
        bias[key] = max(0.0, min(MAX_BIAS, weight))
    if dropped:
        _LOG.warning("dropped bias keys not present in any block id: %s",
                     sorted(dropped))
    spec.bias = bias

    spec.validate()
    return spec


def from_description_llm(
    text: str,
    seed: int = 1,
    host: str = DEFAULT_HOST,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    catalogue_path: str | Path | None = "data/catalogue2/catalogue.ndjson",
    fallback: bool = True,
) -> MapSpec:
    """Translate ``text`` into a :class:`MapSpec` using a local LLM.

    Falls back to the keyword translator on any failure when
    ``fallback`` is set, so callers never lose the ability to generate.
    """
    try:
        content = _ollama_chat(_prompt(text), host, model, timeout)
        raw = json.loads(content)
        if not isinstance(raw, dict):
            raise ValueError(f"model returned {type(raw).__name__}, not an object")
        spec = _coerce(raw, text, seed, known_bias_keys(catalogue_path))
        _LOG.info(
            "llm spec (%s): family=%s length=%d cp=%d bias=%s",
            model, spec.family, spec.length, spec.checkpoint_every, spec.bias,
        )
        return spec
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _LOG.warning("LLM host unreachable (%s); using keyword translator", exc)
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        _LOG.warning("LLM returned unusable output (%s); using keyword "
                     "translator", exc)
    if not fallback:
        raise RuntimeError(f"LLM translation failed for {text!r}")
    return from_description(text, seed=seed)
