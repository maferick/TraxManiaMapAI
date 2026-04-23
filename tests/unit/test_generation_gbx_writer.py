"""Phase 2 PR H — unit tests for the GBX writer orchestrator.

Tests the Python-side logic (UID derivation, DB lookup, subprocess
wiring) with a stubbed :class:`SubprocessParser`. The actual GBX
round-trip — build the C# binary, emit a real .Map.Gbx, re-parse it —
is a live smoke documented in the PR body; it needs ``dotnet build``
in the loop and doesn't belong in unit tests.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.generation import gbx_writer
from src.generation.gbx_writer import (
    GbxEmitError,
    _derive_map_name,
    _derive_map_uid,
    emit_gbx_from_artifact,
    emit_gbx_from_artifact_file,
)
from src.parsers.base import ParseResult
from src.parsers.errors import ParseErrorCode, ParseStatus


# ---------------------------------------------------------------------
# UID / name derivation
# ---------------------------------------------------------------------

class TestDeriveMapUid:
    def test_is_27_chars_url_safe(self) -> None:
        uid = _derive_map_uid("0c5a545ee271b4a1")
        assert len(uid) == 27
        # base64-url-safe alphabet only
        import re
        assert re.fullmatch(r"[A-Za-z0-9_-]+", uid)

    def test_deterministic_for_same_run_id(self) -> None:
        assert _derive_map_uid("abc") == _derive_map_uid("abc")

    def test_different_run_ids_produce_different_uids(self) -> None:
        assert _derive_map_uid("run1") != _derive_map_uid("run2")


class TestDeriveMapName:
    def test_carries_provenance(self) -> None:
        name = _derive_map_name(
            base_title="OriginalPark", run_id="deadbeefcafebabe",
            seed=42, verified=True,
        )
        # Must carry enough context that the operator can tell one
        # generated map from another in a Trackmania map list.
        assert "OriginalPark" in name
        assert "deadbeef" in name           # short run_id
        assert "42" in name                 # seed
        assert "verified" in name

    def test_null_base_title_has_fallback(self) -> None:
        name = _derive_map_name(
            base_title=None, run_id="abcdef0123456789",
            seed=7, verified=False,
        )
        assert name.startswith("generated")
        assert "rejected" in name


# ---------------------------------------------------------------------
# DB lookup
# ---------------------------------------------------------------------

class TestLookupBaseGbx:
    def _stub_conn(self, *, title, raw_path) -> MagicMock:
        cur = MagicMock()
        cur.fetchone.return_value = (title, raw_path)
        ctx = MagicMock()
        ctx.__enter__.return_value = cur
        ctx.__exit__.return_value = False
        conn = MagicMock()
        # The writer uses src.storage.mariadb.cursor(conn), not
        # conn.cursor() — monkeypatch that indirection below.
        return conn, ctx

    def test_returns_title_and_absolute_path(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        real = tmp_path / "fake.Map.Gbx"
        real.write_bytes(b"not a real gbx")
        conn, ctx = self._stub_conn(title="T", raw_path=str(real))
        monkeypatch.setattr(gbx_writer, "cursor", lambda c: ctx)
        title, path = gbx_writer._lookup_base_gbx(conn, 1212)
        assert title == "T"
        assert path == real

    def test_raises_when_map_missing(self, monkeypatch) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = None
        ctx = MagicMock()
        ctx.__enter__.return_value = cur
        ctx.__exit__.return_value = False
        monkeypatch.setattr(gbx_writer, "cursor", lambda c: ctx)
        with pytest.raises(GbxEmitError, match="not found"):
            gbx_writer._lookup_base_gbx(MagicMock(), 999999)

    def test_raises_when_raw_path_null(self, monkeypatch) -> None:
        conn, ctx = self._stub_conn(title="T", raw_path=None)
        monkeypatch.setattr(gbx_writer, "cursor", lambda c: ctx)
        with pytest.raises(GbxEmitError, match="no raw_artifact_path"):
            gbx_writer._lookup_base_gbx(conn, 1)

    def test_raises_when_file_missing_on_disk(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        missing = tmp_path / "nope.Map.Gbx"
        conn, ctx = self._stub_conn(title="T", raw_path=str(missing))
        monkeypatch.setattr(gbx_writer, "cursor", lambda c: ctx)
        with pytest.raises(GbxEmitError, match="missing on disk"):
            gbx_writer._lookup_base_gbx(conn, 1)


# ---------------------------------------------------------------------
# End-to-end orchestrator with mocked subprocess parser
# ---------------------------------------------------------------------

def _valid_artifact(
    *, base_map_id=1212, run_id="0c5a545ee271b4a1",
    seed=42, verified=True,
) -> dict:
    return {
        "schema_version": "generation-v0",
        "run_id": run_id,
        "inputs": {
            "base_map_id": base_map_id,
            "base_map_source_id": "52622",
            "style_tag_filter": None,
            "difficulty": "medium",
            "random_seed": seed,
        },
        "finishability": {
            "route_verified": verified,
            "estimated_time_ms": 11732 if verified else None,
            "ai_confidence": 0.68 if verified else None,
            "reject_reason": None if verified else "demo",
            "gate_version": "finishability-v0",
        },
    }


def _successful_parser_result(out_path: Path, *, block_count=541) -> ParseResult:
    return ParseResult(
        status=ParseStatus.SUCCESS,
        parser_version="0.1.0",
        duration_ms=250,
        output={
            "base_path": "/some/base.Map.Gbx",
            "output_path": str(out_path),
            "new_map_uid": "IGNORED-OVERRIDE",
            "block_count": block_count,
            "baked_block_count": 1024,
        },
    )


class TestEmitGbxFromArtifact:
    def test_happy_path(self, tmp_path: Path, monkeypatch) -> None:
        fake_base = tmp_path / "base.Map.Gbx"
        fake_base.write_bytes(b"fake")
        out_dir = tmp_path / "out"

        # Patch DB lookup.
        monkeypatch.setattr(
            gbx_writer, "_lookup_base_gbx",
            lambda conn, mid: ("PLASTICE! 0203!", fake_base),
        )
        # Mock the subprocess parser.
        parser = MagicMock()
        parser.emit_map.return_value = _successful_parser_result(
            out_dir / "base1212-0c5a545ee271b4a1.Map.Gbx",
        )
        result = emit_gbx_from_artifact(
            MagicMock(),
            artifact=_valid_artifact(),
            parser=parser,
            output_dir=out_dir,
        )
        assert result.output_path.name == "base1212-0c5a545ee271b4a1.Map.Gbx"
        assert result.block_count == 541
        assert result.new_map_uid  # non-empty
        # Verify the subprocess was called with the right kwargs:
        parser.emit_map.assert_called_once()
        kw = parser.emit_map.call_args.kwargs
        assert kw["base_path"] == fake_base
        assert kw["output_path"].parent == out_dir
        assert "PLASTICE!" in kw["map_name"]
        assert "42" in kw["map_name"]
        assert "verified" in kw["map_name"]

    def test_reject_artifact_emits_with_rejected_marker(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # Reject-path artifacts still have a base_map_id, so emission
        # is legal; the title marker just says "rejected" so the
        # operator doesn't load it thinking it's verified.
        fake_base = tmp_path / "base.Map.Gbx"
        fake_base.write_bytes(b"fake")
        monkeypatch.setattr(
            gbx_writer, "_lookup_base_gbx",
            lambda conn, mid: ("BaseTitle", fake_base),
        )
        parser = MagicMock()
        parser.emit_map.return_value = _successful_parser_result(
            tmp_path / "out" / "x.Map.Gbx",
        )
        result = emit_gbx_from_artifact(
            MagicMock(),
            artifact=_valid_artifact(verified=False),
            parser=parser,
            output_dir=tmp_path / "out",
        )
        kw = parser.emit_map.call_args.kwargs
        assert "rejected" in kw["map_name"]
        assert "verified" not in kw["map_name"]

    def test_null_base_map_id_rejected(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        art = _valid_artifact()
        art["inputs"]["base_map_id"] = None
        with pytest.raises(GbxEmitError, match="base_map_id is null"):
            emit_gbx_from_artifact(
                MagicMock(), artifact=art, parser=MagicMock(),
                output_dir=tmp_path,
            )

    def test_missing_run_id_rejected(self, tmp_path: Path) -> None:
        art = _valid_artifact()
        del art["run_id"]
        with pytest.raises(GbxEmitError, match="run_id"):
            emit_gbx_from_artifact(
                MagicMock(), artifact=art, parser=MagicMock(),
                output_dir=tmp_path,
            )

    def test_wrapper_failure_propagates(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        fake_base = tmp_path / "base.Map.Gbx"
        fake_base.write_bytes(b"fake")
        monkeypatch.setattr(
            gbx_writer, "_lookup_base_gbx",
            lambda conn, mid: ("T", fake_base),
        )
        parser = MagicMock()
        parser.emit_map.return_value = ParseResult(
            status=ParseStatus.FAILED_PERMANENT,
            parser_version="0.1.0",
            duration_ms=12,
            error_code=ParseErrorCode.CORRUPT_BODY,
            error_detail="fake wrapper failure",
        )
        with pytest.raises(GbxEmitError, match="wrapper emit-map failed"):
            emit_gbx_from_artifact(
                MagicMock(),
                artifact=_valid_artifact(),
                parser=parser,
                output_dir=tmp_path,
            )

    def test_from_file_convenience(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        fake_base = tmp_path / "base.Map.Gbx"
        fake_base.write_bytes(b"fake")
        artifact_path = tmp_path / "artifact.json"
        artifact_path.write_text(json.dumps(_valid_artifact()), encoding="utf-8")
        monkeypatch.setattr(
            gbx_writer, "_lookup_base_gbx",
            lambda conn, mid: ("T", fake_base),
        )
        parser = MagicMock()
        parser.emit_map.return_value = _successful_parser_result(
            tmp_path / "out" / "x.Map.Gbx",
        )
        result = emit_gbx_from_artifact_file(
            MagicMock(), artifact_path=artifact_path, parser=parser,
            output_dir=tmp_path / "out",
        )
        assert result.block_count == 541
