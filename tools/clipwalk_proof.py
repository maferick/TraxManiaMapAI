"""Generate a clip-matched proof map and emit it as .Map.Gbx.

The first end-to-end consumer of the block catalogue: ClipWalker
builds a start-to-finish RoadTech route (straights + curves +
checkpoints), and the GBX wrapper's emit-map-from-blocks writes a
playable map. Success criterion: the route meshes cleanly in-game —
the exact failure mode that forced generator v0.6 down to
straight-only wall chains.

Usage:
    python tools/clipwalk_proof.py --seed 42 --length 40 \
        --out "%USERPROFILE%/Documents/Trackmania/Maps/My Maps/ClipWalkProof.Map.Gbx"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.catalogue.loader import load_catalogue
from src.generation.clip_walker import WALKER_VERSION, ClipWalker

_LOG = logging.getLogger("clipwalk_proof")

ROADTECH_SET = [
    "RoadTechStart",
    "RoadTechFinish",
    "RoadTechCheckpoint",
    "RoadTechStraight",
    "RoadTechCurve1",
    "RoadTechCurve2",
    "RoadTechCurve3",
    "RoadTechChicaneX2Left",
    "RoadTechChicaneX2Right",
    "RoadTechChicaneX3Left",
    "RoadTechChicaneX3Right",
    "RoadTechSlopeBase",
    "RoadTechSlopeBase2",
]

_UID_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
)


def _map_uid(seed: int, length: int) -> str:
    """Deterministic 27-char UID over the run inputs."""
    digest = hashlib.sha256(
        f"{WALKER_VERSION}:{seed}:{length}".encode()
    ).digest()
    return "".join(_UID_ALPHABET[b % len(_UID_ALPHABET)] for b in digest[:27])


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue", default="data/catalogue/catalogue.ndjson")
    parser.add_argument("--template", default="data/catalogue/template48.Map.Gbx")
    parser.add_argument("--wrapper",
                        default="parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper.dll")
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--length", type=int, default=40)
    parser.add_argument("--checkpoint-every", type=int, default=12)
    parser.add_argument(
        "--priors", type=str, default=None,
        help="face_priors_v1 JSON (export-face-priors); enables "
             "corpus-weighted candidate ordering",
    )
    parser.add_argument(
        "--supports", action="store_true",
        help="fill columns under elevated route blocks with pillars",
    )
    parser.add_argument(
        "--pillar-rules", default="data/catalogue/pillar_rules.json",
        help="game-harvested pillar table (harvest_pillar_rules.py)",
    )
    args = parser.parse_args()

    catalogue = load_catalogue(args.catalogue)
    priors = None
    if args.priors:
        from src.generation.priors import FacePriors
        priors = FacePriors.from_json(args.priors)
    walker = ClipWalker(catalogue, ROADTECH_SET, seed=args.seed, priors=priors)
    placements = walker.generate(args.length, args.checkpoint_every)
    _LOG.info("route: %d blocks (seed=%d)", len(placements), args.seed)

    if args.supports:
        from src.generation.supports import PillarRules, build_supports
        pillars = build_supports(
            placements, catalogue, PillarRules.load(args.pillar_rules)
        )
        # Route first: supports are appended, never displacing route
        # blocks, and the route list above is already final.
        placements = placements + pillars
        _LOG.info("with supports: %d blocks total", len(placements))

    request = {
        "base_path": str(Path(args.template).resolve()),
        "output_path": str(Path(args.out).expanduser().resolve()),
        "map_uid": _map_uid(args.seed, args.length),
        "map_name": f"ClipWalk proof s{args.seed} l{args.length}",
        "blocks": [
            {
                "block_family": "Road",
                "block_name": p.block_id,
                "x": p.x,
                "y": p.y,
                "z": p.z,
                "rotation": p.rotation,
                **({} if p.variant is None else {"variant": p.variant}),
            }
            for p in placements
        ],
    }

    proc = subprocess.run(
        ["dotnet", str(Path(args.wrapper).resolve()), "emit-map-from-blocks"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        _LOG.error("wrapper crashed: %s", proc.stderr.strip())
        return 1
    envelope = json.loads(proc.stdout)
    if envelope.get("status") != "success":
        _LOG.error("wrapper error: %s", envelope)
        return 1

    out = envelope["output"]
    _LOG.info(
        "emitted %s (placed=%s skipped=%s baked=%s)",
        out["output_path"], out["placed_block_count"],
        out["skipped_block_count"], out["baked_block_count"],
    )
    if out["placed_block_count"] != len(placements):
        _LOG.error("placement count mismatch — inspect before driving")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
