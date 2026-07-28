"""Successor preference mined from DRIVEN routes, for the grammar walker.

Why this exists when SEQUENCE_WEIGHT is defaulted to 0: that prior was
mined from map GEOMETRY, where order is inferred, and its strongest
patterns were same-block runs, so every weight tried rewarded
repetition (14 distinct -> 11, top share 0.31 -> 0.40). This prior is
mined from watched playback (driven_block_visits), where the order is
real, and the data says the opposite of what geometry implied:
same-type share of driven transitions is 5%, and same-type runs have
mean length 1.65. Real routes change block type nearly every step.

Three structural guards, each answering a measured failure:

* **Distinct types only.** Same-type rows are excluded at load, so the
  mechanism CANNOT reward repetition, which is the §9 failure mode.
* **Breadth, not depth.** Scores use n_maps, never n. The corpus's top
  transition by raw n is one map bouncing between two slope pieces 800
  times; by map count it is nowhere near the top.
* **Normalised share, bounded multiplier.** Per from-type, the score is
  n_maps divided by the largest n_maps among that type's successors, so
  the bonus lives in [0, 1] and cannot dominate legality or breadth
  weighting the way an unbounded raw count would.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

_LOG = logging.getLogger(__name__)

# Minimum distinct maps a transition must appear in before it counts as
# evidence. Mirrors the promotion threshold in the replay-ground-truth
# contract (>=3 distinct clean replays) and the grammar's own min-maps
# lesson.
MIN_MAPS = 3


class DrivenPrior:
    """(from_type, to_type) -> preference share in [0, 1]."""

    def __init__(self, table: dict[str, dict[str, int]]) -> None:
        self._share: dict[str, dict[str, float]] = {}
        for from_type, succ in table.items():
            best = max(succ.values())
            if best <= 0:
                continue
            self._share[from_type] = {
                to: n / best for to, n in succ.items()
            }

    @classmethod
    def load(cls, path: str | Path) -> "DrivenPrior":
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        table: dict[str, dict[str, int]] = {}
        for t in doc.get("transitions", ()):
            if t.get("link") != "contact":
                continue
            if t["from"] == t["to"]:
                continue
            if int(t.get("maps") or 0) < MIN_MAPS:
                continue
            row = table.setdefault(t["from"], {})
            # A (from, to) pair appears once per (rel_rot, delta); fold
            # to type level by taking the max map support of any form.
            row[t["to"]] = max(row.get(t["to"], 0), int(t["maps"]))
        prior = cls(table)
        _LOG.info(
            "driven prior: %d from-types, %d edges (link=contact, "
            "distinct types, >=%d maps)",
            len(prior._share),
            sum(len(v) for v in prior._share.values()),
            MIN_MAPS,
        )
        return prior

    def share(self, from_type: str, to_type: str) -> float:
        if from_type == to_type:
            return 0.0
        return self._share.get(from_type, {}).get(to_type, 0.0)
