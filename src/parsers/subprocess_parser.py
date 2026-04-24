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

    def probe_pak(self, pak_path: Path) -> ParseResult:
        """Invoke the wrapper's ``probe-pak`` command.

        #217-M2a — capability probe for NadeoPak (.pak) archives.
        TM2020 ships block data in ``Packs/Stadium.pak`` rather than
        loose ``.Block.Gbx`` files; before we build a pak walker we
        need empirical confirmation that GBX.NET.PAK opens the
        operator's pak without a key and can enumerate the block
        entries.

        Returns a success ``output`` with ``pak_version``, ``title_id``,
        ``file_count``, ``block_gbx_count``, and a sample of block
        entries (path + size + encrypted flag) — or a structured error
        if the pak is encrypted / unsupported / malformed.

        This call does NOT decompress individual entries; it only
        reads the pak directory.
        """
        return self._invoke("probe-pak", f"{pak_path}\n")

    def dump_block_info(self, block_gbx_path: Path) -> ParseResult:
        """Invoke the wrapper's ``dump-block-info`` command.

        #217-M1 — reads one ``.Block.Gbx`` file from the TM2020 game
        data and returns its footprint data:

            {
              "block_id": "PlatformPlasticWallStraight4",
              "name": ..., "collection": "Stadium", "author": "Nadeo",
              "has_ground": true, "has_air": false,
              "ground_units": [[0,0,0], [1,0,0], [2,0,0], [3,0,0]],
              "air_units":    [],
              "ground_variant_count": 1,
              "air_variant_count": 0
            }

        ``ground_units`` / ``air_units`` are lists of `[dx, dy, dz]`
        relative cell offsets from the block's placement origin —
        the real footprint the strip policy needs.

        The operator runs this across their TM2020 install to build
        a ``block_catalog`` table; generation-time lookups avoid
        re-parsing the .Block.Gbx each time.
        """
        return self._invoke("dump-block-info", f"{block_gbx_path}\n")

    def emit_map(
        self,
        *,
        base_path: Path,
        output_path: Path,
        map_uid: str,
        map_name: str,
        keep_cells: list[tuple[int, int, int]] | None = None,
    ) -> ParseResult:
        """Invoke the wrapper's ``emit-map`` command.

        PR H / copy-from-base: loads ``base_path``'s .Map.Gbx, rewrites
        MapUid + MapName, saves to ``output_path``.

        Level-2 strip-to-route: when ``keep_cells`` is supplied, the
        wrapper drops every grid block whose Coord isn't in the set
        before Save. Free blocks + BakedBlocks are untouched.

        Returns a :class:`ParseResult` shaped the same as the parse
        commands so callers can treat the subprocess contract
        uniformly — ``output`` dict carries ``output_path`` +
        ``new_map_uid`` + block counts (+ ``removed_block_count`` when
        stripping).
        """
        payload_dict: dict[str, Any] = {
            "base_path": str(base_path),
            "output_path": str(output_path),
            "map_uid": map_uid,
            "map_name": map_name,
        }
        if keep_cells is not None:
            payload_dict["keep_cells"] = [
                [int(c[0]), int(c[1]), int(c[2])] for c in keep_cells
            ]
        return self._invoke("emit-map", json.dumps(payload_dict) + "\n")

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
