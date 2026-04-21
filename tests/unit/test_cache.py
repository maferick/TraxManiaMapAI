from __future__ import annotations

from pathlib import Path

from src.ingestion.cache import ResponseCache


def test_roundtrip(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    key = ResponseCache.key_for("GET", "https://example.com/a", {"q": 1})
    assert cache.get(key) is None
    cache.put(key, b"hello")
    assert cache.get(key) == b"hello"


def test_key_is_canonical_regardless_of_param_order() -> None:
    k1 = ResponseCache.key_for("GET", "https://x/y", {"a": 1, "b": 2})
    k2 = ResponseCache.key_for("GET", "https://x/y", {"b": 2, "a": 1})
    assert k1 == k2


def test_key_distinguishes_method_and_url() -> None:
    base = {"q": 1}
    k_get = ResponseCache.key_for("GET", "https://x/y", base)
    k_post = ResponseCache.key_for("POST", "https://x/y", base)
    k_other = ResponseCache.key_for("GET", "https://x/z", base)
    assert len({k_get, k_post, k_other}) == 3


def test_put_is_atomic(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    cache.put("deadbeef" * 8, b"v1")
    cache.put("deadbeef" * 8, b"v2")
    assert cache.get("deadbeef" * 8) == b"v2"


def test_key_hex_length() -> None:
    key = ResponseCache.key_for("GET", "https://x", None)
    assert len(key) == 64
    int(key, 16)  # valid hex


def test_missing_key_returns_none(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    assert cache.get("f" * 64) is None
