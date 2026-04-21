"""Parse-status and parse-error taxonomy.

Values must stay in lockstep with the SQL ENUM declarations in
``migrations/mariadb/003_maps.sql`` and ``005_replays.sql`` and with
the ``error_taxonomy_code`` free-string in ``002_stage_runs.sql``.
Adding a value is a schema migration.
"""
from __future__ import annotations

from enum import Enum


class ParseStatus(str, Enum):
    UNPARSED = "unparsed"
    SUCCESS = "success"
    FAILED_TRANSIENT = "failed_transient"
    FAILED_PERMANENT = "failed_permanent"
    SKIPPED = "skipped"


class ParseErrorCode(str, Enum):
    NONE = "none"
    GBX_READ_ERROR = "gbx_read_error"
    UNSUPPORTED_FORMAT = "unsupported_format"
    CORRUPT_HEADER = "corrupt_header"
    CORRUPT_BODY = "corrupt_body"
    UNKNOWN_BLOCK_TYPE = "unknown_block_type"
    WRAPPER_TIMEOUT = "wrapper_timeout"
    WRAPPER_CRASH = "wrapper_crash"
    IO_ERROR = "io_error"
    UNKNOWN = "unknown"


_TRANSIENT_CODES = frozenset(
    {
        ParseErrorCode.WRAPPER_TIMEOUT,
        ParseErrorCode.WRAPPER_CRASH,
        ParseErrorCode.IO_ERROR,
    }
)


def is_transient(code: ParseErrorCode) -> bool:
    """Whether this error is worth retrying.

    A consistent ``WRAPPER_CRASH`` on the same input becomes permanent
    only after the ingestion retry policy exhausts its budget; the
    taxonomy itself treats it as transient.
    """
    return code in _TRANSIENT_CODES


def status_for_error(code: ParseErrorCode) -> ParseStatus:
    if code is ParseErrorCode.NONE:
        return ParseStatus.SUCCESS
    return ParseStatus.FAILED_TRANSIENT if is_transient(code) else ParseStatus.FAILED_PERMANENT
