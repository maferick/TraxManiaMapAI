from __future__ import annotations

import stat
import textwrap
from pathlib import Path

import pytest

from src.parsers import (
    ParseErrorCode,
    ParseResult,
    ParseStatus,
    SubprocessParser,
    is_transient,
    status_for_error,
)


class TestErrorTaxonomy:
    def test_success_code_is_none(self) -> None:
        assert status_for_error(ParseErrorCode.NONE) is ParseStatus.SUCCESS

    def test_transient_codes(self) -> None:
        assert is_transient(ParseErrorCode.WRAPPER_TIMEOUT)
        assert is_transient(ParseErrorCode.WRAPPER_CRASH)
        assert is_transient(ParseErrorCode.IO_ERROR)

    def test_permanent_codes(self) -> None:
        assert not is_transient(ParseErrorCode.CORRUPT_HEADER)
        assert not is_transient(ParseErrorCode.UNSUPPORTED_FORMAT)
        assert not is_transient(ParseErrorCode.UNKNOWN_BLOCK_TYPE)

    def test_transient_maps_to_failed_transient(self) -> None:
        assert status_for_error(ParseErrorCode.WRAPPER_TIMEOUT) is ParseStatus.FAILED_TRANSIENT

    def test_permanent_maps_to_failed_permanent(self) -> None:
        assert status_for_error(ParseErrorCode.CORRUPT_HEADER) is ParseStatus.FAILED_PERMANENT


class TestParseResultValidation:
    def test_success_requires_output(self) -> None:
        with pytest.raises(ValueError, match="output"):
            ParseResult(
                status=ParseStatus.SUCCESS,
                parser_version="1.0.0",
                duration_ms=1,
                error_code=ParseErrorCode.NONE,
                output=None,
            )

    def test_success_rejects_error_code(self) -> None:
        with pytest.raises(ValueError, match="NONE"):
            ParseResult(
                status=ParseStatus.SUCCESS,
                parser_version="1.0.0",
                duration_ms=1,
                error_code=ParseErrorCode.CORRUPT_HEADER,
                output={"ok": True},
            )

    def test_error_requires_error_code(self) -> None:
        with pytest.raises(ValueError, match="error_code"):
            ParseResult(
                status=ParseStatus.FAILED_PERMANENT,
                parser_version="1.0.0",
                duration_ms=1,
                error_code=ParseErrorCode.NONE,
            )

    def test_success_happy_path(self) -> None:
        r = ParseResult(
            status=ParseStatus.SUCCESS,
            parser_version="1.0.0",
            duration_ms=5,
            error_code=ParseErrorCode.NONE,
            output={"blocks": []},
        )
        assert r.error_detail is None


def _write_fake_wrapper(path: Path, script: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(script))
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class TestSubprocessParser:
    def test_success_payload(self, tmp_path: Path) -> None:
        wrapper = _write_fake_wrapper(
            tmp_path / "wrap.sh",
            """
            read path
            cat <<'EOF'
            {"status": "success", "parser_version": "1.0.0", "output": {"blocks": []}}
            EOF
            """,
        )
        parser = SubprocessParser(
            executable=wrapper, parser_version="1.0.0", timeout_seconds=5.0
        )
        artifact = tmp_path / "fake.gbx"
        artifact.write_bytes(b"")
        r = parser.parse_map(artifact)
        assert r.status is ParseStatus.SUCCESS
        assert r.output == {"blocks": []}
        assert r.error_code is ParseErrorCode.NONE

    def test_structured_error_payload(self, tmp_path: Path) -> None:
        wrapper = _write_fake_wrapper(
            tmp_path / "wrap.sh",
            """
            read path
            cat <<'EOF'
            {"status":"error","parser_version":"1.0.0","error_code":"corrupt_header","error_detail":"bad magic"}
            EOF
            """,
        )
        parser = SubprocessParser(
            executable=wrapper, parser_version="1.0.0", timeout_seconds=5.0
        )
        r = parser.parse_map(tmp_path / "fake.gbx")
        assert r.status is ParseStatus.FAILED_PERMANENT
        assert r.error_code is ParseErrorCode.CORRUPT_HEADER
        assert r.error_detail == "bad magic"

    def test_unknown_error_code_is_mapped(self, tmp_path: Path) -> None:
        wrapper = _write_fake_wrapper(
            tmp_path / "wrap.sh",
            """
            read path
            cat <<'EOF'
            {"status":"error","parser_version":"1.0.0","error_code":"something_new"}
            EOF
            """,
        )
        parser = SubprocessParser(
            executable=wrapper, parser_version="1.0.0", timeout_seconds=5.0
        )
        r = parser.parse_map(tmp_path / "fake.gbx")
        assert r.error_code is ParseErrorCode.UNKNOWN

    def test_non_zero_exit_is_wrapper_crash(self, tmp_path: Path) -> None:
        wrapper = _write_fake_wrapper(
            tmp_path / "wrap.sh",
            """
            read path
            echo boom >&2
            exit 1
            """,
        )
        parser = SubprocessParser(
            executable=wrapper, parser_version="1.0.0", timeout_seconds=5.0
        )
        r = parser.parse_map(tmp_path / "fake.gbx")
        assert r.error_code is ParseErrorCode.WRAPPER_CRASH
        assert r.status is ParseStatus.FAILED_TRANSIENT

    def test_missing_executable(self, tmp_path: Path) -> None:
        parser = SubprocessParser(
            executable=tmp_path / "does-not-exist",
            parser_version="1.0.0",
            timeout_seconds=5.0,
        )
        r = parser.parse_map(tmp_path / "fake.gbx")
        assert r.error_code is ParseErrorCode.IO_ERROR

    def test_invalid_json_from_wrapper(self, tmp_path: Path) -> None:
        wrapper = _write_fake_wrapper(
            tmp_path / "wrap.sh",
            """
            read path
            echo not-json
            """,
        )
        parser = SubprocessParser(
            executable=wrapper, parser_version="1.0.0", timeout_seconds=5.0
        )
        r = parser.parse_map(tmp_path / "fake.gbx")
        assert r.error_code is ParseErrorCode.UNKNOWN
        assert r.status is ParseStatus.FAILED_PERMANENT

    def test_rejects_nonpositive_timeout(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="timeout"):
            SubprocessParser(
                executable=tmp_path / "x", parser_version="1.0.0", timeout_seconds=0.0
            )

    def test_emit_map_payload_round_trips(self, tmp_path: Path) -> None:
        # emit-map returns the same ParseResult shape as parse-side
        # commands. Verify a success envelope round-trips through the
        # Python wrapper.
        wrapper = _write_fake_wrapper(
            tmp_path / "emit_wrap.sh",
            r"""
            read line
            cat <<'EOF'
            {"status":"success","parser_version":"1.0.0","output":{
              "base_path":"/b","output_path":"/o","new_map_uid":"UUU",
              "block_count":42,"baked_block_count":13
            }}
            EOF
            """,
        )
        parser = SubprocessParser(
            executable=wrapper, parser_version="1.0.0", timeout_seconds=5.0,
        )
        r = parser.emit_map(
            base_path=Path("/some/base.Map.Gbx"),
            output_path=Path("/tmp/out.Map.Gbx"),
            map_uid="UUU",
            map_name="Test",
        )
        assert r.status is ParseStatus.SUCCESS
        assert r.output["block_count"] == 42
        assert r.output["new_map_uid"] == "UUU"

    def test_emit_map_sends_json_payload(self, tmp_path: Path) -> None:
        # Whitebox: emit_map must produce stdin = JSON line, not a
        # path. The parse-side commands send a bare path; any
        # confusion breaks the wrapper protocol silently.
        import json as _json
        captured_stdin = tmp_path / "stdin-capture"
        wrapper = _write_fake_wrapper(
            tmp_path / "capture.sh",
            rf"""
            cat > "{captured_stdin}"
            cat <<'EOF'
            {{"status":"success","parser_version":"1.0.0","output":{{
              "base_path":"/b","output_path":"/o","new_map_uid":"U",
              "block_count":0,"baked_block_count":0
            }}}}
            EOF
            """,
        )
        parser = SubprocessParser(
            executable=wrapper, parser_version="1.0.0", timeout_seconds=5.0,
        )
        parser.emit_map(
            base_path=Path("/in.Map.Gbx"),
            output_path=Path("/out.Map.Gbx"),
            map_uid="DEADBEEF",
            map_name="title bar",
        )
        stdin_text = captured_stdin.read_text(encoding="utf-8").strip()
        payload = _json.loads(stdin_text)
        assert payload == {
            "base_path": "/in.Map.Gbx",
            "output_path": "/out.Map.Gbx",
            "map_uid": "DEADBEEF",
            "map_name": "title bar",
        }

    def test_emit_map_from_blocks_sends_block_list(self, tmp_path: Path) -> None:
        # v0.2 AI generator path — stdin must carry a JSON envelope
        # with base_path + output_path + map_uid + map_name + blocks[]
        # where each block entry has block_family/block_name/x/y/z/rotation.
        import json as _json
        captured = tmp_path / "stdin-capture"
        wrapper = _write_fake_wrapper(
            tmp_path / "builder_capture.sh",
            rf"""
            cat > "{captured}"
            cat <<'EOF'
            {{"status":"success","parser_version":"1.0.0","output":{{
              "base_path":"/b","output_path":"/o","new_map_uid":"U",
              "input_block_count":2,"placed_block_count":2,
              "skipped_block_count":0,"source_block_count":500,
              "baked_block_count":1800
            }}}}
            EOF
            """,
        )
        parser = SubprocessParser(
            executable=wrapper, parser_version="1.0.0", timeout_seconds=5.0,
        )
        blocks = [
            {
                "block_family": "Platform",
                "block_name": "PlatformPlasticStart",
                "x": 0, "y": 9, "z": 0, "rotation": 0,
                "ai_score": 0.42,  # extra key — wrapper passes it through
            },
            {
                "block_family": "Road",
                "block_name": "RoadTechStraight",
                "x": 1, "y": 9, "z": 0, "rotation": 1,
            },
        ]
        r = parser.emit_map_from_blocks(
            base_path=Path("/base.Map.Gbx"),
            output_path=Path("/out.Map.Gbx"),
            map_uid="DEADBEEF", map_name="ai-1",
            blocks=blocks,
        )
        assert r.status is ParseStatus.SUCCESS
        assert r.output["placed_block_count"] == 2

        payload = _json.loads(captured.read_text(encoding="utf-8").strip())
        # The shim strips extras (ai_score) — only the schema-tracked
        # keys reach the C# side.
        assert payload["base_path"] == "/base.Map.Gbx"
        assert payload["output_path"] == "/out.Map.Gbx"
        assert payload["map_uid"] == "DEADBEEF"
        assert payload["map_name"] == "ai-1"
        assert len(payload["blocks"]) == 2
        b0 = payload["blocks"][0]
        assert b0 == {
            "block_family": "Platform",
            "block_name": "PlatformPlasticStart",
            "x": 0, "y": 9, "z": 0, "rotation": 0,
        }

    def test_timeout(self, tmp_path: Path) -> None:
        wrapper = _write_fake_wrapper(
            tmp_path / "wrap.sh",
            """
            read path
            sleep 5
            """,
        )
        parser = SubprocessParser(
            executable=wrapper, parser_version="1.0.0", timeout_seconds=0.2
        )
        r = parser.parse_map(tmp_path / "fake.gbx")
        assert r.error_code is ParseErrorCode.WRAPPER_TIMEOUT
        assert r.status is ParseStatus.FAILED_TRANSIENT


class TestSubprocessParserDumpBlockInfo:
    """M1 — dump-block-info sits on the same envelope contract as
    the parse-side commands. Full round-trip through a fake wrapper;
    real .Block.Gbx parses need the operator's TM2020 install."""

    def test_success_payload_round_trips(self, tmp_path: Path) -> None:
        wrapper = _write_fake_wrapper(
            tmp_path / "block_wrap.sh",
            r"""
            read line
            cat <<'EOF'
            {"status":"success","parser_version":"1.0.0","output":{
              "block_id":"PlatformPlasticWallStraight4",
              "name":"PlatformPlasticWallStraight4",
              "collection":"Stadium","author":"Nadeo",
              "has_ground":true,"has_air":false,
              "ground_units":[[0,0,0],[1,0,0],[2,0,0],[3,0,0]],
              "air_units":[],
              "ground_variant_count":1,"air_variant_count":0
            }}
            EOF
            """,
        )
        parser = SubprocessParser(
            executable=wrapper, parser_version="1.0.0", timeout_seconds=5.0,
        )
        r = parser.dump_block_info(Path("/some/block.Block.Gbx"))
        assert r.status is ParseStatus.SUCCESS
        assert r.output["block_id"] == "PlatformPlasticWallStraight4"
        assert r.output["ground_units"] == [
            [0, 0, 0], [1, 0, 0], [2, 0, 0], [3, 0, 0],
        ]
        assert r.output["has_ground"] is True
        assert r.output["has_air"] is False

    def test_io_error_for_missing_file(self, tmp_path: Path) -> None:
        # Echo back a structured io_error (mimics the real wrapper's
        # behaviour when the caller points at a nonexistent path).
        wrapper = _write_fake_wrapper(
            tmp_path / "missing_wrap.sh",
            r"""
            read line
            cat <<'EOF'
            {"status":"error","parser_version":"1.0.0","error_code":"io_error","error_detail":"file not found: /x"}
            EOF
            """,
        )
        parser = SubprocessParser(
            executable=wrapper, parser_version="1.0.0", timeout_seconds=5.0,
        )
        r = parser.dump_block_info(Path("/x"))
        # IO errors classify transient in the existing taxonomy
        # (missing paths are operator-fixable by re-ingest or by
        # supplying a valid input path); the load-bearing assertion
        # here is the error_code.
        assert r.error_code is ParseErrorCode.IO_ERROR
        assert r.status is ParseStatus.FAILED_TRANSIENT


class TestSubprocessParserProbePak:
    """M2a — probe-pak sits on the same envelope contract as the
    parse-side commands. Full round-trip through a fake wrapper;
    real Stadium.pak probing needs the operator's TM2020 install."""

    def test_success_payload_round_trips(self, tmp_path: Path) -> None:
        wrapper = _write_fake_wrapper(
            tmp_path / "pak_wrap.sh",
            r"""
            read line
            cat <<'EOF'
            {"status":"success","parser_version":"1.0.0","output":{
              "pak_path":"/games/Trackmania/Packs/Stadium.pak",
              "pak_version":6,"title_id":"TMStadium",
              "is_header_encrypted":false,"is_data_private":false,
              "has_packlist":true,"has_key":true,
              "file_count":12345,"block_gbx_count":842,
              "block_gbx_sample":[
                {"path":"GameCtnBlockInfo/Stadium/Race/RoadTech/RoadTechStraight.Block.Gbx",
                 "size":5120,"compressed_size":3211,"is_encrypted":false,"class_id":"0x2E001000"}
              ]
            }}
            EOF
            """,
        )
        parser = SubprocessParser(
            executable=wrapper, parser_version="1.0.0", timeout_seconds=5.0,
        )
        r = parser.probe_pak(Path("/games/Trackmania/Packs/Stadium.pak"))
        assert r.status is ParseStatus.SUCCESS
        assert r.output["pak_version"] == 6
        assert r.output["title_id"] == "TMStadium"
        assert r.output["block_gbx_count"] == 842
        assert r.output["has_packlist"] is True
        assert r.output["has_key"] is True
        assert len(r.output["block_gbx_sample"]) == 1
        assert r.output["block_gbx_sample"][0]["path"].endswith(".Block.Gbx")

    def test_unsupported_format_for_non_pak(self, tmp_path: Path) -> None:
        # Simulates pointing probe-pak at something that isn't a
        # NadeoPak (e.g., the raw .pak happens to be encrypted in a
        # way GBX.NET.PAK doesn't handle, or the file has been
        # corrupted). The real wrapper's ClassifyError routes
        # NotAPakException → unsupported_format.
        wrapper = _write_fake_wrapper(
            tmp_path / "bad_pak_wrap.sh",
            r"""
            read line
            cat <<'EOF'
            {"status":"error","parser_version":"1.0.0","error_code":"unsupported_format","error_detail":"NotAPakException: magic mismatch"}
            EOF
            """,
        )
        parser = SubprocessParser(
            executable=wrapper, parser_version="1.0.0", timeout_seconds=5.0,
        )
        r = parser.probe_pak(Path("/some/Stadium.pak"))
        assert r.error_code is ParseErrorCode.UNSUPPORTED_FORMAT
        # unsupported_format is permanent — a non-pak file is not
        # going to become a pak on retry.
        assert r.status is ParseStatus.FAILED_PERMANENT
