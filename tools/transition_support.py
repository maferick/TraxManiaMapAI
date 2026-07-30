"""How much of a generated route did humans actually drive?

The editor answers STRUCTURE ("is this a map"). It cannot answer
DRIVABILITY, because TM2020 has no AI driver and validation waits for a
person. This is the strongest drivability evidence obtainable without
one: every joint in a route is checked against the joints observed in
42,965 real driven transitions mined from watched playback.

Three strictness levels, because they license different claims:

  pair    from-type -> to-type was driven somewhere. Weak: says the two
          pieces can follow each other, nothing about the geometry.
  rot     ...at the same relative rotation.
  exact   ...at the same relative rotation AND the same local-frame
          delta. This is "a human drove this precise joint", and is the
          only level that constrains the actual placement.

Circularity guard: the transition table was mined from the same corpus
the baseline sequences come from, so scoring a real route against the
unfiltered table is trivially ~100%. Membership therefore requires the
transition to appear in >= MIN_OTHER_MAPS DISTINCT maps, so a real
route's own map cannot be the sole evidence for its own joints.

The corpus baseline is the point of the tool. "68% of generated joints
were observed" means nothing until you know what real routes score --
real routes are NOT 100%, because mappers keep inventing joints.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

_LOG = logging.getLogger("transition-support")

# A transition counts as observed only if it appears in at least this
# many distinct maps, so a sequence being scored cannot be the only
# evidence for its own joints.
MIN_OTHER_MAPS = 2


def load_tables(path: Path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    pair, rot, exact = set(), set(), set()
    for t in doc["transitions"]:
        if t.get("maps", 0) < MIN_OTHER_MAPS:
            continue
        f, to = t["from"], t["to"]
        pair.add((f, to))
        rot.add((f, to, t["rel_rot"]))
        exact.add((f, to, t["rel_rot"], tuple(t["d"])))
    _LOG.info("table: %d pair / %d rot / %d exact (maps >= %d)",
              len(pair), len(rot), len(exact), MIN_OTHER_MAPS)
    return pair, rot, exact


def _rotate_local(dx: int, dz: int, from_rot: int) -> tuple[int, int]:
    """World XZ delta into the from-block's local frame.

    Must match src/learning/driven_transitions._rotate_local exactly, or
    the 'exact' level compares two different coordinate conventions and
    silently reports near-zero support.
    """
    d = (4 - (from_rot % 4)) % 4
    for _ in range(d):
        dx, dz = -dz, dx
    return dx, dz


def score(tokens, pair, rot, exact):
    """Fraction of consecutive PLACE joints found at each level."""
    places = [t for t in tokens if t["op"] == "PLACE"]
    n = 0
    hits = {"pair": 0, "rot": 0, "exact": 0}
    for a, b in zip(places, places[1:]):
        fa, fb = a["block"], b["block"]
        if fa == fb:
            # Same-type runs are excluded from the mined table by
            # design (they are the repetition failure mode), so scoring
            # them here would count guaranteed misses.
            continue
        n += 1
        ra, rb = int(a.get("rot") or 0), int(b.get("rot") or 0)
        rel = (rb - ra) % 4
        dx, dy, dz = b["d"]
        lx, lz = _rotate_local(dx, dz, ra)
        if (fa, fb) in pair:
            hits["pair"] += 1
        if (fa, fb, rel) in rot:
            hits["rot"] += 1
        if (fa, fb, rel, (lx, dy, lz)) in exact:
            hits["exact"] += 1
    if not n:
        return None
    return {k: v / n for k, v in hits.items()} | {"joints": n}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transitions", type=Path,
                    default=Path("data/artifacts/telemetry/"
                                 "driven_transitions_v0.2.json"))
    ap.add_argument("--corpus", type=Path,
                    default=Path("data/artifacts/telemetry/"
                                 "construction_sequences_v0.3.jsonl"),
                    help="real sequences, for the baseline ceiling")
    ap.add_argument("--samples", type=Path,
                    help="model output to score against that baseline")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    pair, rot, exact = load_tables(args.transitions)

    groups: list[tuple[str, list]] = []
    real = []
    with args.corpus.open(encoding="utf-8") as fh:
        for line in fh:
            if len(real) >= args.limit:
                break
            real.append(json.loads(line)["tokens"])
    groups.append(("real routes", real))

    if args.samples:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tools.realise_generated_map import _parse_lenient
        doc = json.loads(args.samples.read_text(encoding="utf-8"))
        gen = [t for t in (_parse_lenient(s)
                           for s in (doc.get("samples") or []))
               if t]
        groups.append(("generated", gen))

    print()
    print("%-14s %6s %8s %8s %8s %8s" % (
        "group", "seqs", "joints", "pair", "rot", "exact"))
    print("-" * 58)
    for label, seqs in groups:
        rows = [r for r in (score(t, pair, rot, exact) for t in seqs) if r]
        if not rows:
            print("%-14s %6d   (no scorable joints)" % (label, len(seqs)))
            continue
        j = sum(r["joints"] for r in rows)
        print("%-14s %6d %8d %8.3f %8.3f %8.3f" % (
            label, len(rows), j,
            sum(r["pair"] for r in rows) / len(rows),
            sum(r["rot"] for r in rows) / len(rows),
            sum(r["exact"] for r in rows) / len(rows)))
    print()
    print("Read the generated row ONLY against the real row. Real routes")
    print("are not 1.000 -- mappers keep inventing joints -- so the real")
    print("row is the ceiling, not 100%.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
