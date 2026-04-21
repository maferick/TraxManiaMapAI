"""Entrypoint for ``python -m src.benchmarks``.

Delegates to the manifest CLI. See ``src/benchmarks/manifest.py``.
"""
from __future__ import annotations

import sys

from .manifest import _cli

if __name__ == "__main__":
    sys.exit(_cli())
