"""On-disk response cache keyed by SHA-256 of the canonicalized request.

Cache hits are bytes-for-bytes replays of prior responses. Only 2xx
responses are cached; errors are never persisted. See
``HttpClient.get`` for the policy.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Mapping


class ResponseCache:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(method: str, url: str, params: Mapping[str, object] | None) -> str:
        canonical = ""
        if params:
            parts = sorted((str(k), str(v)) for k, v in params.items())
            canonical = "&".join(f"{k}={v}" for k, v in parts)
        return hashlib.sha256(f"{method.upper()}\n{url}\n{canonical}".encode("utf-8")).hexdigest()

    def _path_for(self, key: str) -> Path:
        return self._root / key[:2] / key

    def get(self, key: str) -> bytes | None:
        path = self._path_for(key)
        if not path.is_file():
            return None
        return path.read_bytes()

    def put(self, key: str, data: bytes) -> None:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
