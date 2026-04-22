from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.replay.breadcrumbs import (
    BREADCRUMBS_SCHEMA_VERSION,
    BreadcrumbsFormatError,
    BreadcrumbsLoadError,
    FileBreadcrumbLoader,
    InputEvent,
    ReplayBreadcrumbs,
    from_dict,
)


def _valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "source_replay_id": "r1",
        "player_login": "tester",
        "finish_time_ms": 60_000,
        "checkpoint_times_ms": [10_000, 25_000, 45_000, 60_000],
        "inputs": [
            {"time_ms": 0, "kind": "Accelerate", "repr": "..."},
            {"time_ms": 500, "kind": "SteerTM2020", "repr": "..."},
        ],
        "inputs_count": 2,
    }
    payload.update(overrides)
    return payload


def test_from_dict_happy_path() -> None:
    bc = from_dict(_valid_payload())
    assert bc.schema_version == BREADCRUMBS_SCHEMA_VERSION
    assert bc.source_replay_id == "r1"
    assert bc.finish_time_ms == 60_000
    assert bc.checkpoint_times_ms == (10_000, 25_000, 45_000, 60_000)
    assert len(bc.inputs) == 2
    assert bc.inputs[0].kind == "Accelerate"
    assert bc.duration_ms == 60_000


def test_duration_falls_back_to_last_checkpoint_when_finish_missing() -> None:
    bc = from_dict(_valid_payload(finish_time_ms=None))
    assert bc.duration_ms == 60_000  # last checkpoint


def test_duration_none_when_both_missing() -> None:
    bc = from_dict(_valid_payload(finish_time_ms=None, checkpoint_times_ms=[]))
    assert bc.duration_ms is None


def test_schema_version_mismatch_raises() -> None:
    with pytest.raises(BreadcrumbsFormatError, match="schema_version"):
        from_dict(_valid_payload(schema_version=99))


def test_missing_required_field_raises() -> None:
    payload = _valid_payload()
    del payload["source_replay_id"]
    with pytest.raises(BreadcrumbsFormatError, match="source_replay_id"):
        from_dict(payload)


def test_inputs_must_be_list() -> None:
    with pytest.raises(BreadcrumbsFormatError, match="inputs must be a list"):
        from_dict(_valid_payload(inputs="not-a-list"))


def test_count_inputs_by_kind() -> None:
    bc = ReplayBreadcrumbs(
        schema_version=1,
        source_replay_id="r1",
        inputs=(
            InputEvent(time_ms=0, kind="Accelerate", repr=""),
            InputEvent(time_ms=10, kind="Respawn", repr=""),
            InputEvent(time_ms=20, kind="Respawn", repr=""),
        ),
        inputs_count=3,
    )
    assert bc.count_inputs_by_kind("Respawn") == 2
    assert bc.count_inputs_by_kind("Brake") == 0


def test_file_loader_missing_sidecar_raises(tmp_path: Path) -> None:
    loader = FileBreadcrumbLoader()
    raw = tmp_path / "artifact"
    raw.write_bytes(b"not-a-real-replay")
    with pytest.raises(BreadcrumbsLoadError, match="missing"):
        loader.load_by_path(str(raw))


def test_file_loader_invalid_json(tmp_path: Path) -> None:
    raw = tmp_path / "artifact"
    raw.write_bytes(b"")
    (tmp_path / "artifact.breadcrumbs.json").write_text("not json", encoding="utf-8")
    loader = FileBreadcrumbLoader()
    with pytest.raises(BreadcrumbsLoadError, match="not valid JSON"):
        loader.load_by_path(str(raw))


def test_file_loader_format_validation_error_wrapped(tmp_path: Path) -> None:
    raw = tmp_path / "artifact"
    raw.write_bytes(b"")
    (tmp_path / "artifact.breadcrumbs.json").write_text(
        json.dumps(_valid_payload(schema_version=99)), encoding="utf-8"
    )
    loader = FileBreadcrumbLoader()
    with pytest.raises(BreadcrumbsLoadError, match="format validation"):
        loader.load_by_path(str(raw))


def test_file_loader_happy_path(tmp_path: Path) -> None:
    raw = tmp_path / "artifact"
    raw.write_bytes(b"")
    (tmp_path / "artifact.breadcrumbs.json").write_text(
        json.dumps(_valid_payload()), encoding="utf-8"
    )
    loader = FileBreadcrumbLoader()
    bc = loader.load_by_path(str(raw))
    assert bc.source_replay_id == "r1"
    assert bc.inputs_count == 2
