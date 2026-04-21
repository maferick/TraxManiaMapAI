"""Parser-boundary interface.

The GBX.NET wrapper runs in a separate process; the Python pipeline sees
it only through this ABC. Concrete implementations live alongside
(``subprocess.py``) or in test helpers (``tests/...``).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ParseErrorCode, ParseStatus


@dataclass(frozen=True)
class ParseResult:
    status: ParseStatus
    parser_version: str
    duration_ms: int
    error_code: ParseErrorCode = ParseErrorCode.NONE
    error_detail: str | None = None
    output: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status is ParseStatus.SUCCESS:
            if self.error_code is not ParseErrorCode.NONE:
                raise ValueError("SUCCESS status requires error_code=NONE")
            if self.output is None:
                raise ValueError("SUCCESS status requires a non-None output payload")
        else:
            if self.error_code is ParseErrorCode.NONE and self.status is not ParseStatus.UNPARSED:
                raise ValueError(
                    f"non-SUCCESS status {self.status.value!r} requires a non-NONE error_code"
                )


class ParserClient(ABC):
    """Abstract client for the GBX parser subsystem.

    Subclasses declare a ``parser_version`` class attribute (semver).
    The version is stamped onto every ``ParseResult`` and recorded on
    downstream rows so multiple parser versions can coexist during
    transitions (see ``CLAUDE.md`` / ``docs/data-contracts.md``).
    """

    parser_version: str

    @abstractmethod
    def parse_map(self, artifact_path: Path) -> ParseResult:
        """Parse a GBX map file."""

    @abstractmethod
    def parse_replay(self, artifact_path: Path) -> ParseResult:
        """Parse a GBX replay file."""
