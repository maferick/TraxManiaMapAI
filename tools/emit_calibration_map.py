"""Emit a rotation-calibration map: isolated blocks at all 4 rotations.

The game is the only authority on what Direction 0..3 does to a
block's clip faces. This map places known blocks at known cells and
rotations, far enough apart that nothing auto-connects; a top-down
screenshot then reads as a truth table (open road ends are visible,
capped ends are yellow).

Layout (x grows along the 5-straight marker arm, z grows along the
2-straight arm attached at the marker's low-x end):

    z=8   marker: straights at x=8..12, rotation 0
    z=10  marker: straights at x=8, z=10..11, rotation 0
    z=14  Curve1 at x=8,12,16,20 with rotation 0,1,2,3
    z=18  Straight at x=8,12 with rotation 0,1
    z=22  Start / Checkpoint / Finish at x=8,12,16, rotation 0
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

Y = 9  # ground row, empirical

BLOCKS = (
    # axis marker
    [{"block_name": "RoadTechStraight", "x": 8 + i, "y": Y, "z": 8, "rotation": 0}
     for i in range(5)]
    + [{"block_name": "RoadTechStraight", "x": 8, "y": Y, "z": 10 + i, "rotation": 0}
       for i in range(2)]
    # curve truth table
    + [{"block_name": "RoadTechCurve1", "x": 8 + 4 * d, "y": Y, "z": 14, "rotation": d}
       for d in range(4)]
    # straight orientation check
    + [{"block_name": "RoadTechStraight", "x": 8 + 4 * d, "y": Y, "z": 18, "rotation": d}
       for d in range(2)]
    # gates
    + [
        {"block_name": "RoadTechStart", "x": 8, "y": Y, "z": 22, "rotation": 0},
        {"block_name": "RoadTechCheckpoint", "x": 12, "y": Y, "z": 22, "rotation": 0},
        {"block_name": "RoadTechFinish", "x": 16, "y": Y, "z": 22, "rotation": 0},
    ]
)


def main() -> int:
    out = (Path.home() / "Documents/Trackmania/Maps/My Maps/RotationCalibration.Map.Gbx")
    request = {
        "base_path": str(Path("data/catalogue/template48.Map.Gbx").resolve()),
        "output_path": str(out),
        "map_uid": "RotationCalibration00000042A",
        "map_name": "Rotation calibration",
        "blocks": [{"block_family": "Road", **b} for b in BLOCKS],
    }
    proc = subprocess.run(
        ["dotnet", "parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper.dll",
         "emit-map-from-blocks"],
        input=json.dumps(request), capture_output=True, text=True, timeout=120,
    )
    print(proc.stdout.strip() or proc.stderr.strip())
    return 0 if '"status":"success"' in proc.stdout else 1


if __name__ == "__main__":
    raise SystemExit(main())
