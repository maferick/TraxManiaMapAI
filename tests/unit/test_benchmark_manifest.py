from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.benchmarks.manifest import (
    BenchmarkManifest,
    ManifestValidationError,
    _cli,
    load,
)

_VALID_ENTRY: dict[str, Any] = {
    "map_id": "example_map_001",
    "content_hash": "a" * 64,
    "role": "primary",
    "label": {"hand_curated": True},
    "comment": "anchor map",
}

_VALID_MANIFEST: dict[str, Any] = {
    "schema_version": 1,
    "benchmark_id": "example-benchmark",
    "version": 1,
    "category": "strong_tech",
    "ingestion_snapshot": "2026-04-tmx",
    "released_at": "2026-04-21",
    "author": "tester@example.com",
    "rationale": "A placeholder benchmark used exclusively to validate manifest loading.",
    "entries": [_VALID_ENTRY],
}


def _write_manifest(tmp_path: Path, data: dict[str, Any], *, stem: str | None = None) -> Path:
    filename_stem = stem or f"{data['benchmark_id']}-v{data['version']}"
    path = tmp_path / f"{filename_stem}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


class TestLoadValid:
    def test_loads_minimal_valid_manifest(self, tmp_path: Path) -> None:
        path = _write_manifest(tmp_path, _VALID_MANIFEST)
        manifest = load(path)
        assert isinstance(manifest, BenchmarkManifest)
        assert manifest.version_id == "example-benchmark-v1"
        assert manifest.released_at == date(2026, 4, 21)
        assert manifest.entries[0].role == "primary"
        assert manifest.entries[0].content_hash == "a" * 64
        assert manifest.tags == ()
        assert manifest.supersedes is None

    def test_loads_manifest_with_optional_fields(self, tmp_path: Path) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        data["version"] = 2
        data["supersedes"] = "example-benchmark-v1"
        data["notes"] = "v2 adds one more entry."
        data["tags"] = ["phase1", "seed"]
        path = _write_manifest(tmp_path, data)
        manifest = load(path)
        assert manifest.supersedes == "example-benchmark-v1"
        assert manifest.tags == ("phase1", "seed")


class TestSchemaRejections:
    @pytest.mark.parametrize(
        "field",
        [
            "schema_version",
            "benchmark_id",
            "version",
            "category",
            "ingestion_snapshot",
            "released_at",
            "author",
            "rationale",
            "entries",
        ],
    )
    def test_missing_required_field(self, tmp_path: Path, field: str) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        del data[field]
        path = _write_manifest(
            tmp_path, data, stem=f"{_VALID_MANIFEST['benchmark_id']}-v{_VALID_MANIFEST['version']}"
        )
        with pytest.raises(ManifestValidationError):
            load(path)

    def test_unknown_top_level_field(self, tmp_path: Path) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        data["surprise"] = "should be rejected"
        path = _write_manifest(tmp_path, data)
        with pytest.raises(ManifestValidationError):
            load(path)

    def test_bad_category(self, tmp_path: Path) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        data["category"] = "not_a_real_category"
        path = _write_manifest(tmp_path, data)
        with pytest.raises(ManifestValidationError):
            load(path)

    def test_empty_entries(self, tmp_path: Path) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        data["entries"] = []
        path = _write_manifest(tmp_path, data)
        with pytest.raises(ManifestValidationError):
            load(path)

    def test_bad_content_hash(self, tmp_path: Path) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        data["entries"][0]["content_hash"] = "tooshort"
        path = _write_manifest(tmp_path, data)
        with pytest.raises(ManifestValidationError):
            load(path)

    def test_bad_role(self, tmp_path: Path) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        data["entries"][0]["role"] = "bogus"
        path = _write_manifest(tmp_path, data)
        with pytest.raises(ManifestValidationError):
            load(path)

    def test_short_rationale(self, tmp_path: Path) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        data["rationale"] = "short"
        path = _write_manifest(tmp_path, data)
        with pytest.raises(ManifestValidationError):
            load(path)

    def test_bad_released_at(self, tmp_path: Path) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        data["released_at"] = "April 21, 2026"
        path = _write_manifest(tmp_path, data)
        with pytest.raises(ManifestValidationError):
            load(path)

    def test_bad_supersedes_format(self, tmp_path: Path) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        data["supersedes"] = "example-benchmark"
        path = _write_manifest(tmp_path, data)
        with pytest.raises(ManifestValidationError):
            load(path)

    def test_version_zero_rejected(self, tmp_path: Path) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        data["version"] = 0
        path = _write_manifest(tmp_path, data, stem="example-benchmark-v0")
        with pytest.raises(ManifestValidationError):
            load(path)


class TestFilenameCheck:
    def test_rejects_mismatched_filename(self, tmp_path: Path) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        path = _write_manifest(tmp_path, data, stem="wrong-name-v1")
        with pytest.raises(ManifestValidationError, match="filename stem"):
            load(path)

    def test_rejects_wrong_version_in_filename(self, tmp_path: Path) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        path = _write_manifest(tmp_path, data, stem="example-benchmark-v2")
        with pytest.raises(ManifestValidationError, match="filename stem"):
            load(path)


class TestCli:
    def test_validate_returns_zero_on_valid(self, tmp_path: Path) -> None:
        path = _write_manifest(tmp_path, _VALID_MANIFEST)
        assert _cli(["validate", str(path)]) == 0

    def test_validate_returns_nonzero_on_invalid(self, tmp_path: Path) -> None:
        data = copy.deepcopy(_VALID_MANIFEST)
        del data["entries"]
        path = _write_manifest(tmp_path, data)
        assert _cli(["validate", str(path)]) == 1
