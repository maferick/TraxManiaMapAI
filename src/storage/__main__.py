"""Entrypoint for ``python -m src.storage``. Delegates to the migrate CLI."""
from __future__ import annotations

import sys

from .mariadb import _cli

if __name__ == "__main__":
    sys.exit(_cli())
