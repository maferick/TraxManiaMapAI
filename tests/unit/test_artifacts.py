from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.ingestion.artifacts import ArtifactStore


def test_hash_bytes_matches_hashlib() -> None:
    data = b"hello trackmania"
    assert ArtifactStore.hash_bytes(data) == hashlib.sha256(data).hexdigest()


def test_write_is_idempotent(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    digest1, path1 = store.write(b"abc")
    digest2, path2 = store.write(b"abc")
    assert digest1 == digest2
    assert path1 == path2
    assert path1.is_file()


def test_different_content_different_paths(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    _, p1 = store.write(b"one")
    _, p2 = store.write(b"two")
    assert p1 != p2
    assert p1.read_bytes() == b"one"
    assert p2.read_bytes() == b"two"


def test_path_for_rejects_wrong_length(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    with pytest.raises(ValueError, match="64"):
        store.path_for("short")


def test_has_reflects_presence(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    digest, _ = store.write(b"present")
    assert store.has(digest)
    assert not store.has("f" * 64)
