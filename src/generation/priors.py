"""Face-transition priors: generation-time weighting from the corpus.

Wraps an exported slice of ``block_face_transitions`` (see the
``export-face-priors`` CLI) for use by the clip walker. The walker
asks: given that the route just placed ``prev`` at rotation ``r_p``,
how often did corpus mappers continue with ``next`` at rotation
``r_n`` across this clip? Keys use the RELATIVE rotation
``(r_n - r_p) % 4`` so a pattern learned facing north applies at
every heading.

Weighting policy: ``map_count`` (breadth), not ``transition_count``
(volume) — one map spamming a block 131k times must not outvote 900
maps that used a pattern once each. Add-one smoothing keeps unseen
but clip-legal transitions reachable: frequency biases order, it
never gates validity (the repo-wide composition rule).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger(__name__)

SCHEMA = "face_priors_v1"

# Add-one smoothing floor: weight for a transition the corpus never
# exhibited. Non-zero so the walker can still explore it.
UNSEEN_WEIGHT = 1.0


@dataclass(frozen=True)
class FacePriors:
    environment: str
    # (block_a, block_b, clip_id, rel_rotation) -> map_count
    _table: dict[tuple[str, str, str, int], int]

    @classmethod
    def from_json(cls, path: str | Path) -> "FacePriors":
        path = Path(path)
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema") != SCHEMA:
            raise ValueError(
                f"unsupported priors schema: {doc.get('schema')!r}"
            )
        table: dict[tuple[str, str, str, int], int] = {}
        for row in doc.get("priors", []):
            block_a, block_b, clip_id, rel_rotation, _count, map_count = row
            table[(str(block_a), str(block_b), str(clip_id),
                   int(rel_rotation))] = int(map_count)
        _LOG.info(
            "face priors loaded: %d keys, environment=%s, %s",
            len(table), doc.get("environment"), path,
        )
        return cls(environment=str(doc.get("environment", "")), _table=table)

    def weight(
        self,
        prev_block: str,
        prev_rotation: int,
        next_block: str,
        next_rotation: int,
        clip_id: str,
    ) -> float:
        rel = (next_rotation - prev_rotation) % 4
        return UNSEEN_WEIGHT + self._table.get(
            (prev_block, next_block, clip_id, rel), 0
        )

    def __len__(self) -> int:
        return len(self._table)
