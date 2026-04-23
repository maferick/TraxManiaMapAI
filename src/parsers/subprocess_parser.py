"""Subprocess-backed :class:`ParserClient`.

The wrapper binary is an external .NET executable; this module never
links the .NET runtime. Protocol is documented in
``src/parsers/README.md``.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from .base import ParserClient, ParseResult
from .errors import ParseErrorCode, ParseStatus, status_for_error

_LOG = logging.getLogger(__name__)

_WRAPPER_ERROR_CODE_MAP: dict[str, ParseErrorCode] = {
    e.value: e for e in ParseErrorCode if e is not ParseErrorCode.NONE
}


class SubprocessParser(ParserClient):
    """Invoke the GBX wrapper once per artifact.

    Each call spawns a fresh process. This amortizes badly for very
    high-throughput ingestion; see ``docs/architecture.md`` for the
    subprocess-vs-HTTP tradeoff.
    """

    def __init__(
        self,
        *,
        executable: Path,
        parser_version: str,
        timeout_seconds: float,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._executable = executable
        self.parser_version = parser_version
        self._timeout_seconds = timeout_seconds

    def parse_map(self, artifact_path: Path) -> ParseResult:
        return self._invoke("map", f"{artifact_path}\n")

    def parse_replay(self, artifact_path: Path) -> ParseResult:
        return self._invoke("replay", f"{artifact_path}\n")

    def emit_map(
        self,
        *,
        base_path: Path,
        output_path: Path,
        map_uid: str,
        map_name: str,
    ) -> ParseResult:
        """Invoke the wrapper's ``emit-map`` command.

        PR H / copy-from-base: loads ``base_path``'s .Map.Gbx, rewrites
        MapUid + MapName, saves to ``output_path``. Returns a
        :class:`ParseResult` shaped the same as the parse commands so
        callers can treat the subprocess contract uniformly — ``output``
        dict carries ``output_path`` + ``new_map_uid`` + block counts.
        """
        payload = json.dumps({
            "base_path": str(base_path),
            "output_path": str(output_path),
            "map_uid": map_uid,
            "map_name": map_name,
        })
        return self._invoke("emit-map", payload + "\n")

    def _invoke(self, kind: str, stdin: str) -> ParseResult:
        start = time.monotonic()
        try:
            proc = subprocess.run(
                [str(self._executable), kind],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._result_failure(
                ParseErrorCode.WRAPPER_TIMEOUT,
                f"wrapper timed out after {self._timeout_seconds}s",
                start,
            )
        except FileNotFoundError as exc:
            return self._result_failure(
                ParseErrorCode.IO_ERROR,
                f"wrapper executable not found: {exc}",
                start,
            )
        except OSError as exc:
            return self._result_failure(
                ParseErrorCode.IO_ERROR,
                f"OS error invoking wrapper: {exc}",
                start,
            )

        duration_ms = int((time.monotonic() - start) * 1000)

        if proc.returncode != 0:
            return ParseResult(
                status=ParseStatus.FAILED_TRANSIENT,
                parser_version=self.parser_version,
                duration_ms=duration_ms,
                error_code=ParseErrorCode.WRAPPER_CRASH,
                error_detail=f"exit={proc.returncode} stderr={proc.stderr.strip()[:500]}",
            )

        return self._parse_wrapper_payload(proc.stdout, duration_ms)

    def _parse_wrapper_payload(self, stdout: str, duration_ms: int) -> ParseResult:
        try:
            payload: Any = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return ParseResult(
                status=ParseStatus.FAILED_PERMANENT,
                parser_version=self.parser_version,
                duration_ms=duration_ms,
                error_code=ParseErrorCode.UNKNOWN,
                error_detail=f"wrapper emitted non-JSON: {exc.msg}",
            )
        if not isinstance(payload, dict):
            return ParseResult(
                status=ParseStatus.FAILED_PERMANENT,
                parser_version=self.parser_version,
                duration_ms=duration_ms,
                error_code=ParseErrorCode.UNKNOWN,
                error_detail=f"wrapper payload is {type(payload).__name__}, not object",
            )

        wrapper_version = payload.get("parser_version")
        if wrapper_version and wrapper_version != self.parser_version:
            _LOG.warning(
                "parser_version mismatch: config=%s wrapper=%s",
                self.parser_version,
                wrapper_version,
            )

        status_str = payload.get("status")
        if status_str == "success":
            output = payload.get("output")
            if not isinstance(output, dict):
                return ParseResult(
                    status=ParseStatus.FAILED_PERMANENT,
                    parser_version=self.parser_version,
                    duration_ms=duration_ms,
                    error_code=ParseErrorCode.UNKNOWN,
                    error_detail="success payload missing 'output' object",
                )
            return ParseResult(
                status=ParseStatus.SUCCESS,
                parser_version=self.parser_version,
                duration_ms=duration_ms,
                error_code=ParseErrorCode.NONE,
                output=output,
            )
        if status_str == "error":
            code_str = payload.get("error_code")
            code = _WRAPPER_ERROR_CODE_MAP.get(code_str or "", ParseErrorCode.UNKNOWN)
            return ParseResult(
                status=status_for_error(code),
                parser_version=self.parser_version,
                duration_ms=duration_ms,
                error_code=code,
                error_detail=str(payload.get("error_detail", "")) or None,
            )

        return ParseResult(
            status=ParseStatus.FAILED_PERMANENT,
            parser_version=self.parser_version,
            duration_ms=duration_ms,
            error_code=ParseErrorCode.UNKNOWN,
            error_detail=f"unknown wrapper status {status_str!r}",
        )

    def _result_failure(
        self,
        code: ParseErrorCode,
        detail: str,
        start: float,
    ) -> ParseResult:
        return ParseResult(
            status=status_for_error(code),
            parser_version=self.parser_version,
            duration_ms=int((time.monotonic() - start) * 1000),
            error_code=code,
            error_detail=detail,
        )
