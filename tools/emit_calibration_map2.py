"""Calibration map 2: multi-cell anchor + elevation verification.

For each test block at each rotation, place straight stubs at the
cells where the loader's rotation model says its route ports open.
In-game, a correct model shows every stub joined (no yellow dead-end
caps at the joint); a wrong anchor convention shows caps and/or
overlapping meshes at a glance.

Verifies: multi-cell XZ rotation anchoring (Curve2/Curve3, chicane)
and vertical port offsets (SlopeBase2's top exit at +2).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.catalogue.loader import (
    FACE_DELTAS,
    load_catalogue,
    rotate_face,
    rotate_offset,
)

Y = 9
ROUTE_CLIP = "RoadTechFC"

# (block_id, base z row) — each gets rotations 0..3 along x.
TEST_BLOCKS = [
    ("RoadTechCurve2", 8),
    ("RoadTechCurve3", 16),
    ("RoadTechSlopeBase2", 26),
    ("RoadTechChicaneX2Left", 32),
]


def main() -> int:
    catalogue = load_catalogue("data/catalogue/catalogue.ndjson")
    blocks: list[dict] = []

    # Axis marker: 5 straights along +x, 2 along +z at the low corner.
    blocks += [{"block_name": "RoadTechStraight", "x": 2 + i, "y": Y, "z": 2,
                "rotation": 0} for i in range(5)]
    blocks += [{"block_name": "RoadTechStraight", "x": 2, "y": Y, "z": 4 + i,
                "rotation": 0} for i in range(2)]

    for block_id, z_row in TEST_BLOCKS:
        variant = catalogue[block_id].variant("ground", 0)
        for d in range(4):
            anchor = (8 + 9 * d, Y, z_row)
            blocks.append({
                "block_name": block_id,
                "x": anchor[0], "y": anchor[1], "z": anchor[2],
                "rotation": d,
            })
            for port in variant.side_ports():
                if port.clip_id != ROUTE_CLIP:
                    continue
                cell = rotate_offset(port.offset, d, variant.size)
                face = rotate_face(port.face, d)
                delta = FACE_DELTAS[face]
                stub = (
                    anchor[0] + cell[0] + delta[0],
                    anchor[1] + cell[1] + delta[1],
                    anchor[2] + cell[2] + delta[2],
                )
                blocks.append({
                    "block_name": "RoadTechStraight",
                    "x": stub[0], "y": stub[1], "z": stub[2],
                    "rotation": face % 2,
                })

    out = Path.home() / "Documents/Trackmania/Maps/My Maps/Calibration2.Map.Gbx"
    request = {
        "base_path": str(Path("data/catalogue/template48.Map.Gbx").resolve()),
        "output_path": str(out),
        "map_uid": "Calibration2MultiCell000042A",
        "map_name": "Calibration 2: multicell + slope",
        "blocks": [{"block_family": "Road", **b} for b in blocks],
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
