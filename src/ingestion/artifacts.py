"""Content-addressed raw artifact storage on local disk.

Raw map and replay binaries live here; DB rows only store the path
plus content hash (``CLAUDE.md``: "Raw replay telemetry and large
binary artifacts live on the filesystem; the DB stores path
references plus content hashes.").
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def path_for(self, content_hash: str) -> Path:
        if len(content_hash) != 64:
            raise ValueError(f"content_hash must be 64 hex chars, got {len(content_hash)}")
        return self._root / content_hash[:2] / content_hash[2:4] / content_hash

    def write(self, data: bytes) -> tuple[str, Path]:
        """Store bytes under their content hash. Idempotent.

        Returns the hash and final path. Writing the same bytes twice
        is a no-op aside from the hash computation.
        """
        digest = self.hash_bytes(data)
        dest = self.path_for(digest)
        if dest.is_file():
            return digest, dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(dest)
        return digest, dest

    def has(self, content_hash: str) -> bool:
        return self.path_for(content_hash).is_file()
