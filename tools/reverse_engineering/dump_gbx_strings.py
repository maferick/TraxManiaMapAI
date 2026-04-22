#!/usr/bin/env python3
"""Lightweight placeholder for replay RE triage.

Usage:
  python tools/reverse_engineering/dump_gbx_strings.py /path/to/file.Replay.Gbx > strings.txt
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PRINTABLE = re.compile(rb"[ -~]{4,}")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: dump_gbx_strings.py <gbx_file>", file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    data = path.read_bytes()

    for match in PRINTABLE.finditer(data):
        raw = match.group(0)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
        print(f"{match.start():08x}: {text}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
