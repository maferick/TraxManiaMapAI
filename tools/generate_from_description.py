"""Describe a map, get a playable .Map.Gbx.

The end-to-end path, all offline:

    description -> MapSpec -> route -> support pillars -> scenery
                -> emit-map-from-blocks -> .Map.Gbx

Nothing here needs the game running. The editor bridge
(``tools/tm_mcp``) is only ever used to learn rules and verify them;
generation itself stays headless so it can run in batch.

Two walkers build the route, and the spec picks one:

* ``clip`` joins blocks by matching route-clips — the relation the
  game snaps together. Proven, and limited to a single road family.
* ``grammar`` joins them by what the corpus was observed to contain.
  Slower to trust, but it is the only one that can build platform
  surfaces, mix families, or place a clipless gate.

Examples:
    python tools/generate_from_description.py "flowy dirt map, 40 blocks"
    python tools/generate_from_description.py "twisty technical ice, 3 cps" --seed 9
    python tools/generate_from_description.py "long dirt and plastic map"
    python tools/generate_from_description.py --spec myspec.json
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
from src.generation.clip_walker import ClipWalker, RouteDeadEnd
from src.generation.families import resolve
from src.generation.priors import FacePriors
from src.generation.spec import MapSpec, from_description
from src.generation.scenery import DEFAULT_PALETTE, PALETTES, build_scenery
from src.generation.supports import PillarRules, build_supports

_LOG = logging.getLogger("generate")

_UID_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
)


def _map_uid(spec: MapSpec) -> str:
    digest = hashlib.sha256(spec.to_json().encode()).digest()
    return "".join(_UID_ALPHABET[b % len(_UID_ALPHABET)] for b in digest[:27])


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("description", nargs="?", default=None)
    ap.add_argument("--spec", default=None,
                    help="load a MapSpec JSON instead of parsing text")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--llm", action="store_true",
                    help="translate the description with a local LLM "
                         "instead of keyword matching (falls back "
                         "automatically if the host is down)")
    ap.add_argument("--llm-model", default=None)
    ap.add_argument("--llm-host", default=None)
    ap.add_argument("--out", default=None,
                    help="output .Map.Gbx (default: My Maps/<slug>.Map.Gbx)")
    ap.add_argument("--catalogue", default="data/catalogue2/catalogue.ndjson")
    ap.add_argument("--walker", choices=("clip", "grammar"), default=None,
                    help="override the spec's walker. 'clip' joins by "
                         "matching route-clips; 'grammar' joins by what "
                         "the corpus was observed to contain, which is "
                         "what makes platform surfaces, mixed families "
                         "and jumps buildable")
    ap.add_argument("--grammar",
                    default="data/catalogue/placement_grammar.json")
    ap.add_argument("--min-maps", type=int, default=20,
                    help="grammar walker: a move must appear in this "
                         "many distinct corpus maps to be buildable")
    ap.add_argument("--jumps", action="store_true",
                    help="grammar walker: allow gap moves. Off by "
                         "default — a real jump and two unrelated "
                         "blocks three cells apart are the same row, "
                         "and the second kind dominates. Enabling this "
                         "raises the evidence bar for gaps tenfold")
    ap.add_argument("--priors", default="data/catalogue/face_priors.json")
    ap.add_argument("--rules", default="data/catalogue/pillar_rules.json")
    ap.add_argument("--template", default="data/catalogue/template48.Map.Gbx")
    ap.add_argument("--wrapper",
                    default="parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper.dll")
    ap.add_argument("--scenery", type=float, default=0.0,
                    help="vegetation density 0.0-1.0 (0 = none)")
    ap.add_argument(
        "--item-donor", default=None,
        help="map to borrow structurally-complete anchored objects from. "
             "Required for scenery: GBX.NET cannot author chunk "
             "0x03101005, so items are retargeted from real ones "
             "rather than created (see MapBuilder).",
    )
    ap.add_argument("--palette", default=DEFAULT_PALETTE,
                    choices=sorted(PALETTES),
                    help="vegetation palette")
    ap.add_argument("--save-spec", default=None,
                    help="write the resolved spec here (provenance)")
    args = ap.parse_args()

    if args.spec:
        spec = MapSpec.from_json(args.spec)
    elif args.description and args.llm:
        from src.generation.llm_spec import (
            DEFAULT_HOST, DEFAULT_MODEL, from_description_llm,
        )
        spec = from_description_llm(
            args.description, seed=args.seed,
            host=args.llm_host or DEFAULT_HOST,
            model=args.llm_model or DEFAULT_MODEL,
        )
    elif args.description:
        spec = from_description(args.description, seed=args.seed)
    else:
        ap.error("give a description or --spec")

    if args.save_spec:
        Path(args.save_spec).write_text(spec.to_json(), encoding="utf-8")

    catalogue = load_catalogue(args.catalogue, collection="Stadium2020")

    if args.walker:
        spec.walker = args.walker
        spec.validate()

    if spec.walker == "grammar":
        # Joins blocks by what the corpus contains rather than by clip
        # matching, which is what makes platform surfaces, mixed
        # families and jumps buildable at all.
        from src.generation.families import FAMILIES, resolve_pool
        from src.generation.grammar import PlacementGrammar
        from src.generation.grammar_walker import GrammarWalker

        if not Path(args.grammar).is_file():
            _LOG.error(
                "no placement grammar at %s — run "
                "'mine-placement-grammar' then 'export-placement-grammar'",
                args.grammar,
            )
            return 2
        pool = resolve_pool(
            spec.family_list, catalogue, max_footprint=spec.max_footprint
        )
        walker = GrammarWalker(
            catalogue, PlacementGrammar.from_json(args.grammar), pool,
            seed=spec.seed, min_maps=args.min_maps,
            allow_jumps=args.jumps, block_bias=spec.bias,
            # Every surface the description named must actually appear.
            # The bridge family is deliberately NOT in this list: it is
            # added to make the halves meet, not because it was asked
            # for.
            require_prefixes=[
                FAMILIES[n].prefix for n in spec.family_list
            ],
        )
    else:
        block_ids, clips = resolve(
            spec.family_list, catalogue, max_footprint=spec.max_footprint
        )
        priors = (
            FacePriors.from_json(args.priors)
            if spec.use_priors and Path(args.priors).is_file() else None
        )
        walker = ClipWalker(
            catalogue, block_ids, seed=spec.seed,
            route_clips=clips, priors=priors, block_bias=spec.bias,
        )

    try:
        route = walker.generate(spec.length, spec.checkpoint_every)
    except RouteDeadEnd as exc:
        # A heavily-banned style can make the pool too thin to close a
        # route; say so plainly instead of emitting a broken map.
        _LOG.error("could not close a route: %s", exc)
        _LOG.error("try a longer length, fewer bans, or a wider family")
        return 2

    placements = list(route)
    if spec.supports and Path(args.rules).is_file():
        placements += build_supports(
            route, catalogue, PillarRules.load(args.rules)
        )

    items = []
    if args.scenery > 0:
        items = build_scenery(
            route, catalogue, seed=spec.seed,
            palette=args.palette, density=args.scenery,
        )

    donors = (
        [Path(args.item_donor).resolve()] if args.item_donor
        else sorted(Path("data/catalogue").glob("donor_*.Map.Gbx"))
    )
    if items and not donors:
        _LOG.error("no item donor maps found; run scan-item-donors first")
        return 2

    slug = (spec.description or spec.family).lower()
    slug = "".join(c if c.isalnum() else "-" for c in slug).strip("-")[:40]
    out = Path(args.out) if args.out else (
        Path.home() / "Documents/Trackmania/Maps/My Maps"
        / f"{slug or 'generated'}-s{spec.seed}.Map.Gbx"
    )

    request = {
        "base_path": str(Path(args.template).resolve()),
        "output_path": str(out.expanduser().resolve()),
        "map_uid": _map_uid(spec),
        "map_name": (spec.description or f"{spec.family} s{spec.seed}")[:60],
        "blocks": [
            {
                "block_family": "Road",
                "block_name": p.block_id,
                "x": p.x, "y": p.y, "z": p.z, "rotation": p.rotation,
                **({} if p.variant is None else {"variant": p.variant}),
            }
            for p in placements
        ],
        # Donor pool: corpus maps that already contain the species we
        # clone. No single map has them all, so several are pooled.
        "item_template_path": (str(donors[0]) if items and donors else None),
        "item_template_paths": [str(d) for d in donors[1:]] if items else [],
        "items": [
            {"name": it.name, "x": it.x, "y": it.y, "z": it.z,
             "yaw": it.yaw, "pitch": it.pitch, "roll": it.roll}
            for it in items
        ],
    }
    proc = subprocess.run(
        ["dotnet", str(Path(args.wrapper).resolve()), "emit-map-from-blocks"],
        input=json.dumps(request), capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        _LOG.error("wrapper crashed: %s", proc.stderr.strip()[:300])
        return 1
    envelope = json.loads(proc.stdout)
    if envelope.get("status") != "success":
        _LOG.error("wrapper error: %s", envelope)
        return 1

    info = envelope["output"]
    _LOG.info(
        "%s: %d route + %d pillars + %s items -> %s",
        spec.family, len(route), len(placements) - len(route),
        info.get("placed_item_count", 0), info["output_path"],
    )
    if info["placed_block_count"] != len(placements):
        _LOG.error(
            "placed %s of %d blocks — inspect before driving",
            info["placed_block_count"], len(placements),
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
