"""Unit tests for :mod:`src.generation.replay_cells`.

The loader itself is a thin SQL wrapper; core behaviour we pin is
JSON decoding of the ``path_cells`` LONGTEXT column. Integration with
MariaDB is exercised by the generate-map smoke path.
"""
from __future__ import annotations

import pytest

from src.generation.replay_cells import _parse_path_cells


class TestParsePathCells:
    def test_valid_json_list_of_triples(self) -> None:
        raw = "[[1,2,3],[4,5,6]]"
        assert _parse_path_cells(raw) == [(1, 2, 3), (4, 5, 6)]

    def test_bytes_input_accepted(self) -> None:
        # MariaDB drivers sometimes hand LONGTEXT back as bytes.
        assert _parse_path_cells(b"[[0,0,0]]") == [(0, 0, 0)]

    def test_already_decoded_list_passes_through(self) -> None:
        # pymysql can return already-decoded JSON in some configs.
        assert _parse_path_cells([[7, 8, 9]]) == [(7, 8, 9)]

    def test_empty_and_none_return_empty(self) -> None:
        assert _parse_path_cells("") == []
        assert _parse_path_cells(None) == []
        assert _parse_path_cells(0) == []

    def test_invalid_json_returns_empty(self) -> None:
        assert _parse_path_cells("{not-json") == []

    def test_non_list_top_level_returns_empty(self) -> None:
        assert _parse_path_cells('{"foo":"bar"}') == []

    def test_malformed_entries_skipped(self) -> None:
        raw = "[[1,2,3],[1,2],[1,2,3,4],\"oops\",[4,5,6]]"
        # Only the two well-formed triples survive.
        assert _parse_path_cells(raw) == [(1, 2, 3), (4, 5, 6)]

    def test_non_numeric_entries_skipped(self) -> None:
        raw = '[[1,2,3],["a","b","c"]]'
        assert _parse_path_cells(raw) == [(1, 2, 3)]
