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
