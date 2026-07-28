"""Reversible text form of construction-token sequences.

The LM trains on plain text, so the corpus needs a serialization that
is compact, unambiguous, and round-trips exactly: any sampled output
must be parseable back into construction tokens or it cannot be
evaluated, let alone emitted as a map.

Format, one sequence per line:

    #len=med P RoadTechStraight 0 0 1 2 P RoadTechCurve1 1 0 0 3 J ...

* ``P <name> <dx> <dy> <dz> <rot>``  grid placement
* ``F <name> <dx> <dy> <dz> <rot>``  free placement (cell-quantized)
* ``V <back>``                        revisit, bounded back-reference
* ``J`` / ``G``                       jump / gap traversal context
* ``#len=<short|med|long>``           conditioning header (place count)

Block names may contain spaces (custom blocks); spaces are swapped for
``~``, which no block name in the 1,182-type vocabulary contains. The
loader asserts that stays true rather than assuming it.
"""
from __future__ import annotations

from typing import Any, Iterable

ESCAPE = "~"

LEN_BUCKETS = (("short", 40), ("med", 100), ("long", 10 ** 9))


def _bucket(places: int) -> str:
    for name, cap in LEN_BUCKETS:
        if places < cap:
            return name
    return "long"


def _escape(name: str) -> str:
    if ESCAPE in name:
        raise ValueError(f"block name contains escape char: {name!r}")
    return name.replace(" ", ESCAPE)


def _unescape(name: str) -> str:
    return name.replace(ESCAPE, " ")


def serialize(tokens: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    places = 0
    for t in tokens:
        op = t["op"]
        if op == "PLACE":
            places += 1
            d = t["d"]
            parts.append(" ".join([
                "F" if t.get("free") else "P",
                _escape(t["block"]),
                str(d[0]), str(d[1]), str(d[2]), str(int(t.get("rot") or 0)),
            ]))
        elif op == "REVISIT":
            parts.append(f"V {int(t['back'])}")
        elif op == "JUMP":
            parts.append("J")
        elif op == "GAP":
            parts.append("G")
        else:
            raise ValueError(f"unknown op {op!r}")
    return f"#len={_bucket(places)} " + " ".join(parts)


class ParseError(ValueError):
    pass


def parse(line: str) -> list[dict[str, Any]]:
    """Text back to construction tokens. Strict: garbage raises.

    Strictness is the point. A sampled sequence that does not parse is
    a model failure to COUNT, not to silently repair; lenient parsing
    here would inflate every downstream quality number.
    """
    fields = line.strip().split()
    if not fields:
        raise ParseError("empty line")
    i = 0
    if fields[0].startswith("#len="):
        i = 1
    out: list[dict[str, Any]] = []
    n = len(fields)
    while i < n:
        op = fields[i]
        if op in ("P", "F"):
            if i + 5 >= n:
                raise ParseError(f"truncated {op} at field {i}")
            try:
                d = [int(fields[i + 2]), int(fields[i + 3]), int(fields[i + 4])]
                rot = int(fields[i + 5])
            except ValueError as exc:
                raise ParseError(f"bad numbers in {op} at field {i}") from exc
            if not 0 <= rot <= 3:
                raise ParseError(f"rotation {rot} out of range")
            out.append({
                "op": "PLACE",
                "block": _unescape(fields[i + 1]),
                "d": d,
                "rot": rot,
                "free": op == "F",
            })
            i += 6
        elif op == "V":
            if i + 1 >= n:
                raise ParseError("truncated V")
            try:
                back = int(fields[i + 1])
            except ValueError as exc:
                raise ParseError("bad V argument") from exc
            if back < 1:
                raise ParseError(f"V back-reference {back} < 1")
            out.append({"op": "REVISIT", "back": back})
            i += 2
        elif op == "J":
            out.append({"op": "JUMP"})
            i += 1
        elif op == "G":
            out.append({"op": "GAP"})
            i += 1
        else:
            raise ParseError(f"unknown op {op!r} at field {i}")
    return out
