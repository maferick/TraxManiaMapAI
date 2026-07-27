"""Sequence priors and known jumps, for generation.

The placement grammar answers "what may follow A". This answers the
two questions it cannot:

**What comes NEXT, given where we came from.** Mappers do not pick
blocks pairwise. The corpus's strongest three-block runs are
recognisable patterns — ``Straight x3`` in 274 maps,
``SpecialTurbo2 x3`` in 83 (boosters come in runs, not singly),
``Curve1 -> Straight -> Straight`` in 59, ``SlopeStraight x3`` in 46 —
and a bigram model can only ever reproduce the marginal of those. A
triple prior reproduces the pattern.

**Which gaps are CANDIDATE jumps.** Not from physics: from the fact
that every corpus map was published and parses, so it can be driven.
Candidates rather than observations — the axiom licenses only the gaps
the successful run actually crossed, and an open-end pair may be
scenery, an unused route or a parallel section. Replay extraction
promotes them. See ``src/constraints/route_jumps.py``.

Triples are direction-AMBIGUOUS — ``(A,B,C)`` and ``(C,B,A)`` are the
same physical run, because they were mined from opposing faces rather
than a reconstructed driving order. That is the right shape for a
walker, which asks exactly "standing on B having arrived from A, what
goes on the far side?"
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger(__name__)

SCHEMA = "route_model_v1"


@dataclass(frozen=True)
class Jump:
    block: str
    offset: tuple[int, int, int]
    rel_rotation: int
    gap: int
    map_count: int


class RouteModel:
    def __init__(
        self,
        triples: dict[tuple[str, str], dict[tuple, int]],
        jumps: dict[str, tuple[Jump, ...]],
        environment: str,
    ) -> None:
        self._triples = triples
        self._jumps = jumps
        self.environment = environment

    @classmethod
    def from_json(cls, path: str | Path) -> "RouteModel":
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if doc.get("schema") != SCHEMA:
            raise ValueError(f"unsupported route model: {doc.get('schema')!r}")
        triples: dict[tuple[str, str], dict[tuple, int]] = {}
        for a, b, c, dx, dy, dz, rel, maps in doc.get("triples", ()):
            triples.setdefault((a, b), {})[(c, dx, dy, dz, rel)] = maps
        jumps: dict[str, list[Jump]] = {}
        for a, b, dx, dy, dz, rel, gap, maps in doc.get("jumps", ()):
            jumps.setdefault(a, []).append(
                Jump(b, (dx, dy, dz), rel, gap, maps)
            )
        ordered = {
            k: tuple(sorted(v, key=lambda j: -j.map_count))
            for k, v in jumps.items()
        }
        _LOG.info(
            "route model: %d triple contexts, %d blocks with jumps (%s)",
            len(triples), len(ordered), doc.get("environment", "?"),
        )
        return cls(triples, ordered, str(doc.get("environment", "")))

    def sequence_score(
        self,
        before: str | None,
        current: str,
        candidate: str,
        offset: tuple[int, int, int],
        rel_rotation: int,
    ) -> float:
        """This run's share of its context, 0..1.

        NORMALISED, not the raw map_count. Multiplying a move weight by
        the raw count crushes everything: the top triple is attested in
        274 maps, so even a 0.1 coefficient is a 28x boost, and the
        walker collapses onto a handful of patterns. Measured against
        the corpus (median 21 distinct route block types per map, the
        most-used block 29% of the line), the raw form pushed distinct
        types DOWN from 16 to 11 and the top-block share UP from 0.29
        to 0.36 — worse on both counts than no prior at all.
        """
        if before is None:
            return 0.0
        context = self._triples.get((before, current))
        if not context:
            return 0.0
        seen = context.get(
            (candidate, offset[0], offset[1], offset[2], rel_rotation), 0
        )
        if not seen:
            return 0.0
        return seen / max(context.values())

    def sequence_weight(
        self,
        before: str | None,
        current: str,
        candidate: str,
        offset: tuple[int, int, int],
        rel_rotation: int,
    ) -> int:
        """How many maps run ``before -> current -> candidate`` like this.

        Zero when the run is unattested, which is a signal to weight
        down, never to forbid: the corpus is a sample, and refusing
        everything it has not seen would collapse the vocabulary to the
        few most popular patterns.
        """
        if before is None:
            return 0
        context = self._triples.get((before, current))
        if not context:
            return 0
        return context.get(
            (candidate, offset[0], offset[1], offset[2], rel_rotation), 0
        )

    def jumps_from(self, block: str, min_maps: int = 1) -> tuple[Jump, ...]:
        return tuple(
            j for j in self._jumps.get(block, ()) if j.map_count >= min_maps
        )

    def __len__(self) -> int:
        return len(self._triples)
