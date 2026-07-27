"""Does PlaceBlock overwrite an overlapping block, or refuse?

Two probe pairs came back as "the game allowed a placement our model
says overlaps". If PlaceBlock silently overwrites, both blocks report
placed while the first is partly destroyed — which would mean the
probe harness's acceptance count is optimistic for overlapping pairs,
and that the disagreements are a harness artifact rather than a
geometry finding.

Needs TM2020 with the map editor open on a scratch map.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.verify_map_in_game import call

_LOG = logging.getLogger("overwrite")

# Cells (0,0,0) and (1,0,0) are shared by these two, computed offline.
A = {"name": "RoadTechDiagRightCheckpoint", "x": 20, "y": 14, "z": 20, "dir": 0}
B = {"name": "PlatformPlasticCurve2", "x": 20, "y": 14, "z": 19, "dir": 2}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    state = call("state", timeout=20.0)
    if not state.get("editor_open"):
        _LOG.error("open the map editor first (a blank scratch map)")
        return 1
    call("clear", timeout=120.0)

    first = call("place_blocks", blocks=[A], timeout=120.0)
    _LOG.info("placed A: %s failed=%s", first.get("placed"), first.get("failed"))
    second = call("place_blocks", blocks=[B], timeout=120.0)
    _LOG.info("placed B: %s failed=%s", second.get("placed"), second.get("failed"))

    dump = call("dump_blocks", timeout=120.0)
    names = sorted({
        b["name"] for b in dump.get("blocks", []) if b["name"] != "Grass"
    })
    _LOG.info("map now holds: %s", names)
    survived = A["name"] in names
    _LOG.info(
        "A survived an overlapping B: %s -> %s",
        survived,
        "PlaceBlock does NOT overwrite; the disagreement is real geometry"
        if survived else
        "PlaceBlock OVERWRITES; the probe's acceptance count is optimistic",
    )
    call("clear", timeout=120.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
