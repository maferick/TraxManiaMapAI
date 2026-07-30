"""Turn a generated construction sequence into a real .Map.Gbx and ask
the game whether it is a map.

Every number reported about the LM so far is a PROXY: block-type
distributions, top-block share, parse rate. None of them answer the only
question that matters for a map generator, which is whether the output
is a map at all. This closes that loop:

    generated tokens -> absolute block coords -> .Map.Gbx
                     -> loaded in the editor -> validation verdict

What the verdict can and cannot say (measured, see AIRouteTelemetry's
header): TM2020 has NO AI driver. `Validate()` yields

    NotValidable  the game REJECTS the topology (no Start, no Finish,
                  unlinked checkpoints). A genuine automated negative.
    Validable     structure accepted, waiting for a human to drive.
    Validated     someone actually completed a run.

So an unattended run settles at Validable. "Structurally valid" is real
and worth having; "finishable" is NOT obtainable this way, and must not
be reported as if it were.

Coordinate reconstruction: PLACE deltas are relative to the previous
placement, so absolute positions come from accumulating them. REVISIT
moves the cursor back to an earlier placement without emitting a block,
matching the exporter's semantics. JUMP and GAP are traversal context
and place nothing.
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

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "construction_text",
    Path(__file__).resolve().parents[1] / "src" / "learning"
    / "construction_text.py")
_ct = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_ct)
parse = _ct.parse

_LOG = logging.getLogger("realise")

# Same file-drop channel verify_map_in_game.py uses. Imported lazily:
# tools.tm_mcp.server pulls in the `mcp` package, which the training
# venv does not have, and the emit half of this tool must work without
# the game or the MCP stack installed.
def _channel():
    try:
        from tools.tm_mcp.server import STORAGE, PROTOCOL
        return STORAGE, PROTOCOL
    except ModuleNotFoundError:
        return (Path.home() / "OpenplanetNext" / "PluginStorage"
                / "TMMapControl"), "tm_mcp_v1"


GRID_X = GRID_Z = 48
GRID_Y = 40
GROUND_Y = 9          # calibrated ground plane, see block_matcher.to_cell


def tokens_to_blocks(tokens):
    """Construction tokens -> absolute grid placements.

    Two passes. The tokens carry only RELATIVE deltas, so absolute
    position is neither recoverable nor meaningful: a sequence
    determines the SHAPE of a route, not where on the 48x48x40 base it
    sits. Pass one accumulates from an arbitrary origin; pass two
    translates the bounding box to fit -- centred in x/z, dropped so the
    lowest block rests on the ground plane.

    Fixing an origin up front instead (the obvious first cut) discards
    real geometry: it cost 17 of 45 blocks on a REAL corpus route, which
    is a bug in the reconstruction, not a property of the route.

    Blocks still outside after translation are dropped and counted, not
    clamped -- a clamped block is a different map, and the point here is
    to test what was actually produced.
    """
    raw = []
    placed = []                      # first-visit order, for REVISIT
    cur = [0, 0, 0]
    for t in tokens:
        op = t["op"]
        if op == "PLACE":
            d = t["d"]
            cur = [cur[0] + d[0], cur[1] + d[1], cur[2] + d[2]]
            raw.append((tuple(cur), t))
            placed.append(tuple(cur))
        elif op == "REVISIT":
            back = int(t["back"])
            if 1 <= back <= len(placed):
                cur = list(placed[-back])
    if not raw:
        return [], {"out_of_bounds": 0, "span": (0, 0, 0)}

    xs = [c[0] for c, _ in raw]
    ys = [c[1] for c, _ in raw]
    zs = [c[2] for c, _ in raw]
    span = (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1,
            max(zs) - min(zs) + 1)

    # A sequence whose span exceeds the grid CANNOT be emitted
    # faithfully, and MUST NOT be emitted mutilated. Earlier this
    # dropped the overflow blocks and the verdict scored the mutilated
    # map — which mislabelled the result as pure model failure. That
    # framing was WRONG (owner caught it 2026-07-30): 24 of 4,086 REAL
    # corpus maps exceed 48x48x40 (grid-block spans to 164x150x90 —
    # offzone/air builds), so oversize output is partly a learned
    # style the grid emitter simply cannot express yet. Report it as
    # its own outcome instead of silently destroying the map.
    if span[0] > GRID_X or span[1] > GRID_Y or span[2] > GRID_Z:
        return [], {"out_of_bounds": len(raw), "span": span,
                    "oversize": True}

    off = ((GRID_X - span[0]) // 2 - min(xs),
           GROUND_Y - min(ys),
           (GRID_Z - span[2]) // 2 - min(zs))
    # Vertical fallback: a tall-but-fitting map anchored at the ground
    # row can still poke out the top; anchor to fit instead.
    if GROUND_Y - min(ys) + max(ys) >= GRID_Y:
        off = (off[0], (GRID_Y - span[1]) - min(ys), off[2])

    blocks = []
    for (x, y, z), t in raw:
        blocks.append({
            "block_family": "",
            "block_name": t["block"],
            "x": x + off[0], "y": y + off[1], "z": z + off[2],
            "rotation": int(t.get("rot") or 0),
        })
    return blocks, {"out_of_bounds": 0, "span": span}


def _parse_lenient(text):
    """Parse a generation, falling back to its longest valid prefix.

    Even v0.4 truncates some sequences. A truncated map is still a map
    worth testing, so keep the prefix rather than discarding the sample:
    discarding would quietly bias the verdict toward the generations
    that happened to terminate cleanly.
    """
    try:
        return parse(text)
    except Exception:
        pass
    fields = text.strip().split()
    body = fields[1:] if fields and fields[0].startswith("#len=") else fields
    for n in range(len(body), 0, -1):
        try:
            return parse(" ".join(body[:n]))
        except Exception:
            continue
    return None


def tm_call(op, timeout=240.0, **payload):
    STORAGE, PROTOCOL = _channel()
    if not STORAGE.is_dir():
        raise SystemExit(f"TMMapControl storage missing: {STORAGE}")
    cid = uuid.uuid4().hex[:12]
    cmd, res = STORAGE / f"{cid}.cmd.json", STORAGE / f"{cid}.res.json"
    cmd.write_text(json.dumps({"protocol": PROTOCOL, "op": op, **payload}),
                   encoding="utf-8")
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if res.is_file():
            try:
                out = json.loads(res.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                time.sleep(0.3)
                continue
            for p in (cmd, res):
                try:
                    p.unlink()
                except OSError:
                    pass
            return out
        time.sleep(0.5)
    cmd.unlink(missing_ok=True)
    raise SystemExit(f"no response to '{op}' in {timeout:.0f}s")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", type=Path,
                    help="sample_*.json written by sample_construction_lm")
    ap.add_argument("--from-corpus", type=Path,
                    help="CONTROL: realise real driven routes from a "
                         "construction_sequences jsonl instead of model "
                         "output. If real routes do not come back "
                         "Validable, the fault is in this pipeline and no "
                         "verdict on the model is meaningful.")
    ap.add_argument("--base", type=Path,
                    default=Path("data/catalogue/template48.Map.Gbx"),
                    help="template .Map.Gbx supplying Stadium metadata")
    ap.add_argument("--out-dir", type=Path,
                    default=(Path.home() / "Documents" / "Trackmania"
                             / "Maps" / "AIGen"),
                    help="where to write generated .Map.Gbx (game Maps dir)")
    ap.add_argument("--wrapper", type=str,
                    default="parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper.dll")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--no-validate", action="store_true",
                    help="emit files only, do not drive the editor")
    args = ap.parse_args()

    import subprocess

    # Build a common work list of (label, tokens) so the control and the
    # model output go through byte-identical downstream code.
    work: list[tuple[str, list]] = []
    if args.from_corpus:
        tag = "Real"
        with args.from_corpus.open(encoding="utf-8") as fh:
            for line in fh:
                if len(work) >= args.limit:
                    break
                rec = json.loads(line)
                work.append((rec.get("map_uid", "?"), rec["tokens"]))
    else:
        if not args.samples:
            raise SystemExit("need --samples or --from-corpus")
        tag = "AIGen"
        doc = json.loads(args.samples.read_text(encoding="utf-8"))
        for text in (doc.get("samples") or [])[: args.limit]:
            toks = _parse_lenient(text)
            if toks is None:
                _LOG.warning("unparseable sample, skipping")
                continue
            work.append(("", toks))

    args.out_dir.mkdir(parents=True, exist_ok=True)

    # Vocabulary clamp. MEASURED (AIGen21, 2026-07-29): one hallucinated
    # block name in 73 made the whole map unloadable, and the game
    # raised a modal error dialog that stalled the rest of the batch.
    # A name outside the Stadium2020 catalogue is a MODEL failure and
    # must surface as its own verdict, not burn a game load.
    vocab: set[str] = set()
    cat_path = Path("data/catalogue2/catalogue.ndjson")
    if cat_path.is_file():
        with cat_path.open(encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                if (rec.get("type") == "block"
                        and rec.get("collection") == "Stadium2020"):
                    vocab.add(rec["name"])

    results = []
    for i, (src, tokens) in enumerate(work, start=1):
        name = f"{tag}{i:02d}"
        if vocab:
            bad = sorted({t["block"] for t in tokens
                          if t["op"] == "PLACE" and t["block"] not in vocab})
            if bad:
                _LOG.error("%s: hallucinated block(s) %s — not emitted",
                           name, ", ".join(bad[:3]))
                results.append({
                    "sample": i, "name": name, "source": src,
                    "tokens": len(tokens), "blocks_in": 0, "placed": 0,
                    "out_of_bounds": 0, "span": (0, 0, 0),
                    "validation": f"HALLUCINATED({bad[0]})",
                })
                continue

        blocks, st = tokens_to_blocks(tokens)
        if st.get("oversize"):
            _LOG.error("%s: span %dx%dx%d exceeds the %dx%dx%d grid — "
                       "not emitted", name, *st["span"],
                       GRID_X, GRID_Y, GRID_Z)
            results.append({
                "sample": i, "name": name, "source": src,
                "tokens": len(tokens), "blocks_in": 0, "placed": 0,
                "out_of_bounds": st["out_of_bounds"], "span": st["span"],
                "validation": "OVERSIZE(%dx%dx%d)" % st["span"],
            })
            continue
        if not blocks:
            _LOG.warning("sample %d: produced no placeable blocks", i)
            continue
        out_path = (args.out_dir / f"{name}.Map.Gbx").resolve()
        payload = {
            "base_path": str(args.base.resolve()),
            "output_path": str(out_path),
            "map_uid": uuid.uuid4().hex[:27],
            "map_name": name,
            "blocks": blocks,
        }
        proc = subprocess.run(
            ["dotnet", args.wrapper, "emit-map-from-blocks"],
            input=json.dumps(payload), capture_output=True, text=True)
        try:
            env = json.loads(proc.stdout)
        except json.JSONDecodeError:
            _LOG.error("sample %d: wrapper gave no JSON (%s)",
                       i, proc.stderr[:120])
            continue
        if env.get("status") != "success":
            _LOG.error("sample %d: emit failed: %s", i, env.get("error_code"))
            continue
        placed = env["output"]["placed_block_count"]

        row = {
            "sample": i, "name": name, "source": src,
            "tokens": len(tokens),
            "blocks_in": len(blocks),
            "placed": placed,
            "out_of_bounds": st["out_of_bounds"],
            "span": st["span"],
        }

        if not args.no_validate:
            # MEASURED, and it silently corrupted a whole control run:
            # when the title API is not ready, EditMap() does nothing,
            # load_map finds the PREVIOUS editor still open, sees a
            # non-null Challenge and reports ok=true. Five maps were
            # then "validated" that were all really map one.
            #
            # ok=true is therefore not sufficient. The only trustworthy
            # confirmation is that the editor came back holding the map
            # we asked for, so check the returned name and treat a
            # mismatch as a hard failure rather than a verdict.
            loaded = tm_call(
                "load_map", map_file=f"{args.out_dir.name}\\{name}.Map.Gbx")
            got = loaded.get("map_name")
            row["loaded"] = bool(loaded.get("ok"))
            row["editor_blocks"] = loaded.get("blocks")
            row["title_ready"] = loaded.get("title_ready")
            if not loaded.get("ok"):
                row["validation"] = "load_failed"
            elif got != name:
                _LOG.error(
                    "%s: editor came back holding '%s' (title_ready=%s, "
                    "editor_closed=%s) — STALE, no verdict",
                    name, got, loaded.get("title_ready"),
                    loaded.get("editor_closed"))
                row["validation"] = f"STALE({got})"
            else:
                status = tm_call("status", timeout=120.0)
                row["validation"] = status.get("validation_status")
        results.append(row)
        _LOG.info("%s: %d blocks -> placed %d, validation %s",
                  name, len(blocks), placed, row.get("validation", "-"))

    print()
    print("%-9s %7s %8s %7s %5s %-12s %s" % (
        "map", "tokens", "blocks", "placed", "oob", "span xyz",
        "validation"))
    print("-" * 72)
    for r in results:
        print("%-9s %7d %8d %7d %5d %-12s %s" % (
            r["name"], r["tokens"], r["blocks_in"], r["placed"],
            r["out_of_bounds"], "%dx%dx%d" % r["span"],
            r.get("validation", "-")))
    print()
    print("NOTE: 'Validable' means the game accepted the STRUCTURE and is")
    print("waiting for a human to drive it. It is NOT proof the map can be")
    print("finished; TM2020 has no AI driver.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
