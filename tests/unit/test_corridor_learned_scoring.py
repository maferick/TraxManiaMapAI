"""Unit tests for learned-score persistence + provenance."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src.corridor.ranking.model import RidgeRegression
from src.corridor.ranking.scoring_pipeline import (
    compute_model_hash,
    load_model_from_report,
)


def _trained_model() -> RidgeRegression:
    m = RidgeRegression(alpha=1.0, feature_names=("bias", "x"))
    m.weights = np.array([0.5, 0.25], dtype=np.float64)
    return m


class TestComputeModelHash:
    def test_deterministic(self) -> None:
        assert compute_model_hash(_trained_model()) == compute_model_hash(_trained_model())

    def test_changes_with_weights(self) -> None:
        a = _trained_model()
        b = _trained_model()
        b.weights = np.array([0.5, 0.26], dtype=np.float64)
        assert compute_model_hash(a) != compute_model_hash(b)

    def test_changes_with_feature_names(self) -> None:
        a = _trained_model()
        b = _trained_model()
        b.feature_names = ("bias", "y")
        assert compute_model_hash(a) != compute_model_hash(b)

    def test_is_sha256_hex(self) -> None:
        h = compute_model_hash(_trained_model())
        assert len(h) == 64
        int(h, 16)  # hex-parseable


class TestLoadModelFromReport:
    def _write_report(
        self, tmp_path: Path, *, has_time: bool, has_inverse: bool,
    ) -> Path:
        scheme_payload = {
            "alpha": 1.0, "feature_names": ["bias", "x"], "weights": [0.1, 0.2],
        }
        payload = {
            "inverse_rank": scheme_payload if has_inverse else None,
            "time_envelope": scheme_payload if has_time else None,
            "map_mean_interval_ms_count": 5,
        }
        p = tmp_path / "report.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return p

    def test_prefers_time_envelope(self, tmp_path: Path) -> None:
        p = self._write_report(tmp_path, has_time=True, has_inverse=True)
        model, tag = load_model_from_report(p)
        assert tag == "time_envelope@0.1.0"
        assert model.weights is not None

    def test_falls_back_to_inverse_rank(self, tmp_path: Path) -> None:
        p = self._write_report(tmp_path, has_time=False, has_inverse=True)
        model, tag = load_model_from_report(p)
        assert tag == "inverse_rank@0.1.0"

    def test_raises_when_neither_scheme_present(self, tmp_path: Path) -> None:
        p = self._write_report(tmp_path, has_time=False, has_inverse=False)
        with pytest.raises(RuntimeError):
            load_model_from_report(p)
