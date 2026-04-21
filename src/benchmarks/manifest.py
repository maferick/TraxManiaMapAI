"""Benchmark manifest loading + validation.

Schema is canonical in ``data/benchmarks/benchmark-manifest.schema.json``.
This module exposes a typed dataclass view plus a small CLI for
``python -m src.benchmarks.manifest validate <path>``.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import jsonschema
import yaml

_SCHEMA_FILENAME = "benchmark-manifest.schema.json"
_BENCHMARKS_DIRNAME = "data"
_LOG = logging.getLogger(__name__)


class ManifestValidationError(Exception):
    """Raised when a manifest fails schema or filename validation."""


@dataclass(frozen=True)
class BenchmarkEntry:
    map_id: str
    content_hash: str
    role: str
    label: dict[str, object]
    comment: str | None = None


@dataclass(frozen=True)
class BenchmarkManifest:
    schema_version: int
    benchmark_id: str
    version: int
    category: str
    ingestion_snapshot: str
    released_at: date
    author: str
    rationale: str
    entries: tuple[BenchmarkEntry, ...]
    supersedes: str | None = None
    notes: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def version_id(self) -> str:
        return f"{self.benchmark_id}-v{self.version}"


def _find_schema_path(start: Path) -> Path:
    for candidate_parent in (start, *start.parents):
        candidate = candidate_parent / _BENCHMARKS_DIRNAME / "benchmarks" / _SCHEMA_FILENAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"could not locate {_SCHEMA_FILENAME} walking up from {start}"
    )


def _load_schema(schema_path: Path | None) -> dict[str, object]:
    if schema_path is None:
        schema_path = _find_schema_path(Path(__file__).resolve())
    with schema_path.open("r", encoding="utf-8") as fh:
        loaded: object = json.load(fh)
    if not isinstance(loaded, dict):
        raise ManifestValidationError(
            f"schema file {schema_path} did not parse as a JSON object"
        )
    return loaded


def _expected_stem(benchmark_id: str, version: int) -> str:
    return f"{benchmark_id}-v{version}"


def _check_filename_matches(path: Path, benchmark_id: str, version: int) -> None:
    expected = _expected_stem(benchmark_id, version)
    if path.stem != expected:
        raise ManifestValidationError(
            f"manifest filename stem {path.stem!r} does not match "
            f"benchmark_id + version {expected!r}"
        )


def _to_dataclass(raw: dict[str, object]) -> BenchmarkManifest:
    released_raw = raw["released_at"]
    if not isinstance(released_raw, (str, date)):
        raise ManifestValidationError(
            f"released_at must be a date or YYYY-MM-DD string, got {type(released_raw).__name__}"
        )
    released_at = (
        released_raw
        if isinstance(released_raw, date)
        else date.fromisoformat(released_raw)
    )
    entries_raw = raw["entries"]
    assert isinstance(entries_raw, list)
    entries = tuple(
        BenchmarkEntry(
            map_id=str(e["map_id"]),
            content_hash=str(e["content_hash"]),
            role=str(e["role"]),
            label=dict(e["label"]) if isinstance(e.get("label"), dict) else {},
            comment=e.get("comment"),
        )
        for e in entries_raw
        if isinstance(e, dict)
    )
    tags_raw = raw.get("tags", [])
    tags = tuple(tags_raw) if isinstance(tags_raw, list) else ()
    return BenchmarkManifest(
        schema_version=int(raw["schema_version"]),  # type: ignore[arg-type]
        benchmark_id=str(raw["benchmark_id"]),
        version=int(raw["version"]),  # type: ignore[arg-type]
        category=str(raw["category"]),
        ingestion_snapshot=str(raw["ingestion_snapshot"]),
        released_at=released_at,
        author=str(raw["author"]),
        rationale=str(raw["rationale"]),
        entries=entries,
        supersedes=(str(raw["supersedes"]) if "supersedes" in raw else None),
        notes=(str(raw["notes"]) if "notes" in raw else None),
        tags=tags,
    )


def _normalize_for_schema(raw: dict[str, Any]) -> dict[str, Any]:
    """YAML natively decodes ``2026-04-21`` to ``datetime.date``; JSON Schema
    validates against ``type: string``. Coerce the single date-valued field
    back to an ISO-8601 string so schema validation sees what the author
    typed, not the post-YAML type.
    """
    normalized = dict(raw)
    released = normalized.get("released_at")
    if isinstance(released, date):
        normalized["released_at"] = released.isoformat()
    return normalized


def load(path: Path, *, schema_path: Path | None = None) -> BenchmarkManifest:
    """Parse and validate a manifest file.

    Raises :class:`ManifestValidationError` if the file fails schema or
    filename-stem checks.
    """
    with path.open("r", encoding="utf-8") as fh:
        raw: object = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ManifestValidationError(
            f"manifest {path} did not parse as a YAML mapping (got {type(raw).__name__})"
        )
    raw = _normalize_for_schema(raw)
    schema = _load_schema(schema_path)
    try:
        jsonschema.validate(instance=raw, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ManifestValidationError(
            f"{path} failed schema validation: {exc.message} (at {list(exc.absolute_path)})"
        ) from exc
    benchmark_id = raw["benchmark_id"]
    version = raw["version"]
    if not isinstance(benchmark_id, str) or not isinstance(version, int):
        raise ManifestValidationError(
            "benchmark_id must be a string and version an integer after schema validation"
        )
    _check_filename_matches(path, benchmark_id, version)
    return _to_dataclass(raw)


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.manifest")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="Validate a manifest file")
    validate.add_argument("path", type=Path)
    validate.add_argument("--schema", type=Path, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "validate":
        try:
            manifest = load(args.path, schema_path=args.schema)
        except (ManifestValidationError, FileNotFoundError, yaml.YAMLError) as exc:
            _LOG.error("invalid: %s", exc)
            return 1
        _LOG.info("valid: %s (%d entries)", manifest.version_id, len(manifest.entries))
        return 0
    return 2
