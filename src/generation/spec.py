"""Map specs — the contract between a description and the generator.

The end goal is "describe a map, get a map". This module owns the
middle: a **MapSpec**, the structured intent that actually drives
generation.

The split is deliberate. Everything here is deterministic and
testable — a spec plus a seed always yields the same map, with no
model in the loop. Translating free text into a spec is a separate,
swappable step:

* :func:`from_description` ships a keyword translator. It has no
  dependencies, runs offline, and handles the phrasings people
  actually use ("flowy dirt map with jumps").
* An LLM translator can replace it later for arbitrary phrasing. It
  would emit the same ``MapSpec``, so nothing downstream changes and
  the generator stays headless.

Style words are grounded in blocks that exist, not invented
adjectives — see ``STYLE_VOCAB``, where each entry maps to
substrings of real Stadium2020 block ids.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from src.generation.families import FAMILIES, SUPPORTED, FamilyError

_LOG = logging.getLogger(__name__)

SCHEMA = "map_spec_v1"

# Block-id substrings, verified present in the Stadium2020 catalogue.
# Weights multiply the corpus prior: >1 prefers, <1 avoids, 0.0 bans.
STYLE_VOCAB: dict[str, dict[str, float]] = {
    # Cornering character
    "flowy": {"Curve2": 4.0, "Curve3": 4.0, "Curve1": 0.3, "Chicane": 0.2},
    "smooth": {"Curve2": 4.0, "Curve3": 4.0, "Curve1": 0.3, "Chicane": 0.2},
    "technical": {"Curve1": 3.0, "Chicane": 3.0, "Curve3": 0.4},
    "tech": {"Curve1": 3.0, "Chicane": 3.0, "Curve3": 0.4},
    "twisty": {"Curve1": 4.0, "Chicane": 3.0},
    "tight": {"Curve1": 4.0, "Chicane": 2.0, "Curve3": 0.2},
    # Speed character
    "fast": {"Straight": 3.0, "Curve3": 2.0, "SpecialTurbo": 2.5,
             "Chicane": 0.3},
    "fullspeed": {"Straight": 4.0, "Curve3": 2.5, "SpecialTurbo": 3.0,
                  "Curve1": 0.2, "Chicane": 0.1},
    "slow": {"Curve1": 2.0, "Chicane": 2.0, "SpecialTurbo": 0.0},
    # Elevation
    "hilly": {"Slope": 4.0},
    "flat": {"Slope": 0.0},
    "climb": {"Slope": 4.0},
    # Hazards / gimmicks
    "boost": {"SpecialBoost": 4.0, "SpecialTurbo": 3.0},
    "turbo": {"SpecialTurbo": 4.0, "SpecialBoost": 3.0},
    "nasty": {"PenaltyIce": 3.0, "PenaltyDirt": 3.0, "SpecialFragile": 3.0},
    "tricky": {"PenaltyIce": 2.5, "SpecialFragile": 2.5, "SpecialReset": 2.0},
    "clean": {"Penalty": 0.0, "SpecialFragile": 0.0, "SpecialNoEngine": 0.0,
              "SpecialReset": 0.0},
    "wavy": {"Wave": 4.0},
}

# Words that select a surface family. The platform entries are only
# buildable by the grammar walker — clip matching cannot chain their
# gates (see families._PLATFORM_REASON).
FAMILY_WORDS: dict[str, str] = {
    "tech": "tech", "concrete": "tech", "road": "tech", "asphalt": "tech",
    "dirt": "dirt", "mud": "dirt", "rally": "dirt",
    "bump": "bump", "bumpy": "bump", "sausage": "bump",
    "ice": "ice", "icy": "ice", "slippery": "ice",
    "water": "water", "wet": "water",
    "plastic": "platform-plastic", "bouncy": "platform-plastic",
    "platform": "platform-tech", "stunt": "platform-tech",
    "grass": "platform-grass",
}

# Rough length words -> block count.
LENGTH_WORDS: dict[str, int] = {
    "tiny": 20, "short": 30, "medium": 60, "long": 100, "huge": 150,
}


class SpecError(ValueError):
    pass


@dataclass
class MapSpec:
    """Structured generation intent. Deterministic with ``seed``."""

    family: str = "tech"
    # Several families in one map. Empty means "just ``family``".
    # Only the grammar walker can honour more than one — clip matching
    # rejects mixed pools, which is a fact about clips rather than
    # about the game (map 4269 runs platform into open road into road
    # in three consecutive cells).
    families: list[str] = field(default_factory=list)
    # "clip" joins blocks by matching route-clips; "grammar" joins
    # them by what the corpus was observed to contain.
    walker: str = "clip"
    length: int = 60
    checkpoint_every: int = 15
    seed: int = 1
    max_footprint: int = 3
    supports: bool = True
    use_priors: bool = True
    # Block-id substring -> weight multiplier.
    bias: dict[str, float] = field(default_factory=dict)
    # Free-text this spec came from, for provenance.
    description: str = ""

    @property
    def family_list(self) -> list[str]:
        return list(self.families) if self.families else [self.family]

    def validate(self) -> None:
        if self.walker not in ("clip", "grammar"):
            raise SpecError(f"unknown walker {self.walker!r}")
        names = self.family_list
        unknown = [n for n in names if n not in FAMILIES]
        if unknown:
            raise SpecError(
                f"unknown families {unknown}; available: {sorted(FAMILIES)}"
            )
        if self.walker == "clip":
            blocked = [n for n in names if FAMILIES[n].unsupported]
            if blocked:
                raise FamilyError(
                    f"{blocked[0]}: {FAMILIES[blocked[0]].unsupported}. "
                    f"Supported by the clip walker: {SUPPORTED}"
                )
            if len(names) > 1:
                raise SpecError(
                    f"the clip walker builds one family at a time, got "
                    f"{names}; use walker='grammar' to mix them"
                )
        if self.length < 3:
            raise SpecError(f"length {self.length} is too short to close a route")
        if self.checkpoint_every < 0:
            raise SpecError("checkpoint_every cannot be negative")
        if self.max_footprint < 1:
            raise SpecError("max_footprint must be >= 1")
        for needle, weight in self.bias.items():
            if weight < 0:
                raise SpecError(f"bias for {needle!r} is negative")

    def to_json(self) -> str:
        return json.dumps({"schema": SCHEMA, **asdict(self)}, indent=2)

    @classmethod
    def from_json(cls, text_or_path: str | Path) -> "MapSpec":
        raw = Path(text_or_path).read_text(encoding="utf-8") \
            if Path(str(text_or_path)).is_file() else str(text_or_path)
        doc = json.loads(raw)
        if doc.get("schema") not in (None, SCHEMA):
            raise SpecError(f"unsupported spec schema: {doc.get('schema')!r}")
        doc.pop("schema", None)
        spec = cls(**doc)
        spec.validate()
        return spec


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def from_description(text: str, seed: int = 1) -> MapSpec:
    """Translate free text into a :class:`MapSpec` by keyword.

    Deliberately simple and dependency-free: it recognises the words
    in ``STYLE_VOCAB`` / ``FAMILY_WORDS`` / ``LENGTH_WORDS`` and
    composes their biases (multiplying where several words touch the
    same block group). Unrecognised words are ignored rather than
    guessed at, and the result records which words it understood.

    An LLM translator would replace this function, not the spec.
    """
    words = _tokens(text)
    spec = MapSpec(seed=seed, description=text.strip())

    matched: list[str] = []

    # Every named family is kept, in order of first mention. "dirt and
    # plastic" is a real request, not an ambiguity — the grammar
    # walker builds it. ``family`` stays set to the last mention so a
    # clip-walker run still has one to use.
    named = [FAMILY_WORDS[w] for w in words if w in FAMILY_WORDS]
    if named:
        spec.families = list(dict.fromkeys(named))
        spec.family = named[-1]
        matched.extend(w for w in words if w in FAMILY_WORDS)
        if len(spec.families) > 1 or FAMILIES[spec.family].unsupported:
            # Neither is buildable by clip matching.
            spec.walker = "grammar"

    for w in words:
        if w in LENGTH_WORDS:
            spec.length = LENGTH_WORDS[w]
            matched.append(w)

    # Explicit block count ("40 blocks") overrides a length word.
    m = re.search(r"(\d+)\s*(?:blocks?|long)", text.lower())
    if m:
        spec.length = int(m.group(1))
        matched.append(m.group(0))

    m = re.search(r"(\d+)\s*(?:checkpoints?|cps?)", text.lower())
    if m and int(m.group(1)) > 0:
        # Spread the requested checkpoints across the route.
        spec.checkpoint_every = max(1, spec.length // (int(m.group(1)) + 1))
        matched.append(m.group(0))

    bias: dict[str, float] = {}
    for w in words:
        for needle, weight in STYLE_VOCAB.get(w, {}).items():
            # Compose: two words both nudging the same group multiply,
            # and any 0.0 (a ban) wins over a preference.
            if weight == 0.0 or bias.get(needle) == 0.0:
                bias[needle] = 0.0
            else:
                bias[needle] = bias.get(needle, 1.0) * weight
        if w in STYLE_VOCAB:
            matched.append(w)
    spec.bias = bias

    if "flat" in words:
        spec.max_footprint = spec.max_footprint  # no-op, kept for clarity

    unknown = [
        w for w in words
        if w not in STYLE_VOCAB and w not in FAMILY_WORDS
        and w not in LENGTH_WORDS and len(w) > 3
    ]
    _LOG.info(
        "spec from description: family=%s length=%d bias=%s (understood %s%s)",
        spec.family, spec.length, spec.bias, sorted(set(matched)),
        f", ignored {sorted(set(unknown))}" if unknown else "",
    )
    spec.validate()
    return spec
