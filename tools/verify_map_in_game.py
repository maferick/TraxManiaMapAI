"""Check a generated route against the game, not against my assumptions.

Every structural bug in the generator so far survived offline checks
and was caught by a human loading the map:

* blocks meeting at a corner with nothing to drive across — passed
  "all cells distinct" and "no overlaps"
* a route coiled into a 20x5 pile — passed connectivity
* road ends the game caps with a dead-end barrier — passed everything

All three are questions ``CGameEditorPluginMap.CanPlaceBlock`` answers
directly. This asks it, per block, before anyone has to look at
anything.

Two modes:

    --spec / description   check a route the generator would build
    --map-file             load a saved .Map.Gbx and read it back

The second is the stronger test: it opens the artifact that would
actually be played, reports the editor's validation status, and dumps
what the game holds — including support pillars the game generated
itself, which the offline emitter has to reproduce by hand.

Requires TM2020 running with the TMMapControl plugin. The MCP server
is not involved; this speaks the same file-drop protocol directly.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.tm_mcp.server import STORAGE, PROTOCOL  # noqa: E402

_LOG = logging.getLogger("verify-map")


def call(op: str, timeout: float = 120.0, **payload) -> dict:
    if not STORAGE.is_dir():
        raise SystemExit(
            f"plugin storage not found: {STORAGE}\n"
            "Is TM2020 running with the TMMapControl plugin loaded?"
        )
    cmd_id = uuid.uuid4().hex[:12]
    cmd = STORAGE / f"{cmd_id}.cmd.json"
    res = STORAGE / f"{cmd_id}.res.json"
    cmd.write_text(
        json.dumps({"protocol": PROTOCOL, "op": op, **payload}),
        encoding="utf-8",
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if res.is_file():
            try:
                out = json.loads(res.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                time.sleep(0.25)
                continue
            for p in (cmd, res):
                try:
                    p.unlink()
                except OSError:
                    pass
            return out
        time.sleep(0.25)
    cmd.unlink(missing_ok=True)
    raise SystemExit(
        f"no response to '{op}' within {timeout:.0f}s — is the editor open?"
    )


def _route_from_spec(args) -> list:
    from src.catalogue.loader import load_catalogue
    from src.generation.families import resolve_pool
    from src.generation.grammar import PlacementGrammar
    from src.generation.grammar_walker import GrammarWalker
    from src.generation.spec import MapSpec, from_description

    spec = (
        MapSpec.from_json(args.spec) if args.spec
        else from_description(args.description, seed=args.seed)
    )
    catalogue = load_catalogue(args.catalogue, collection="Stadium2020")
    pool = resolve_pool(
        spec.family_list, catalogue, max_footprint=spec.max_footprint
    )
    walker = GrammarWalker(
        catalogue, PlacementGrammar.from_json(args.grammar), pool,
        seed=spec.seed, min_maps=args.min_maps, block_bias=spec.bias,
    )
    return walker.generate(spec.length, spec.checkpoint_every)


def check_route(route) -> int:
    """Ask the game whether each block of a route is placeable."""
    blocks = [
        {"name": p.block_id, "x": p.x, "y": p.y, "z": p.z, "dir": p.rotation}
        for p in route
    ]
    _LOG.info("asking the editor about %d placements", len(blocks))
    out = call("can_place", blocks=blocks, timeout=240.0)
    if not out.get("ok"):
        _LOG.error("plugin error: %s", out.get("error"))
        return 1
    results = out.get("results", [])
    bad = [r for r in results if not r.get("can_place")]
    _LOG.info("legal: %d/%d", out.get("legal", 0), out.get("checked", 0))
    for r in bad[:25]:
        _LOG.error(
            "REFUSED  %-38s @(%s,%s,%s) dir=%s  %s",
            r.get("name"), r.get("x"), r.get("y"), r.get("z"),
            r.get("dir"), r.get("reason", ""),
        )
    if len(bad) > 25:
        _LOG.error("... and %d more", len(bad) - 25)
    return 1 if bad else 0


def check_map_file(map_file: str) -> int:
    _LOG.info("loading %s", map_file)
    out = call("load_map", map_file=map_file, timeout=240.0)
    if not out.get("ok"):
        _LOG.error("could not open: %s", out.get("error"))
        return 1
    _LOG.info("opened %r with %s blocks", out.get("map_name"), out.get("blocks"))

    status = call("status", timeout=60.0)
    verdict = status.get("validation_status")
    _LOG.info("validation status: %s", verdict)
    if verdict == "NotValidable":
        # A real automated negative: the topology is rejected. No
        # driver required to learn this.
        _LOG.error(
            "the game REJECTS this map's topology — missing Start/Finish "
            "or unlinked checkpoints"
        )

    dump = call("dump_blocks", timeout=180.0)
    blocks = dump.get("blocks", [])
    _LOG.info("editor holds %d blocks", len(blocks))
    import collections
    kinds = collections.Counter(b["name"] for b in blocks)
    for name, count in kinds.most_common(12):
        _LOG.info("   %4d  %s", count, name)
    return 0 if verdict != "NotValidable" else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("description", nargs="?", default=None)
    ap.add_argument("--spec", default=None)
    ap.add_argument("--map-file", default=None,
                    help="game-resolvable path, e.g. "
                         "'Maps/My Maps/whatever.Map.Gbx'")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--min-maps", type=int, default=20)
    ap.add_argument("--catalogue", default="data/catalogue2/catalogue.ndjson")
    ap.add_argument("--grammar",
                    default="data/catalogue/placement_grammar.json")
    args = ap.parse_args()

    state = call("state", timeout=20.0)
    if not state.get("ok"):
        _LOG.error("plugin not answering: %s", state)
        return 1

    if args.map_file:
        return check_map_file(args.map_file)
    if not (args.description or args.spec):
        ap.error("give a description, --spec, or --map-file")
    if not state.get("editor_open"):
        _LOG.error("open the map editor first (any map will do)")
        return 1
    return check_route(_route_from_spec(args))


if __name__ == "__main__":
    raise SystemExit(main())
