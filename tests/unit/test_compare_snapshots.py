"""Unit tests for the A/B snapshot comparison renderer.

Exercises the pure rendering/summarization path. The DB-driven
``build_comparison`` orchestrator is left as an integration concern —
tested end-to-end when two snapshots exist on the live DB.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.learning.compare_snapshots import (
    SchemeSummary,
    SnapshotComparison,
    SnapshotSummary,
    _delta,
    _fmt,
    render_markdown,
)


def _mk_scheme(
    name: str,
    *,
    n: int = 500,
    label_stdev: float | None = 0.15,
    pred_stdev: float | None = 0.08,
    rank_corr: float | None = 0.25,
    auc: float | None = 0.80,
) -> SchemeSummary:
    return SchemeSummary(
        label_scheme=name,
        n_labeled=n,
        label_stdev=label_stdev,
        pred_stdev_at_prod_alpha=pred_stdev,
        test_rank_corr_at_prod_alpha=rank_corr,
        auc_at_prod_alpha=auc,
    )


def _mk_snapshot(
    snap: str,
    *,
    corridors: int = 898,
    corridor_maps: int = 206,
    mean_maps: int = 74,
    schemes: list[SchemeSummary] | None = None,
    div_intervals: int = 124,
    heur_div: float = 0.58,
    learn_div: float = 0.54,
) -> SnapshotSummary:
    return SnapshotSummary(
        snapshot_id=snap,
        total_corridors=corridors,
        corridor_owning_maps=corridor_maps,
        maps_with_mean_interval=mean_maps,
        schemes=schemes or [_mk_scheme("time_envelope_v2_weighted")],
        diversity_intervals=div_intervals,
        heuristic_diversity_median=heur_div,
        learned_diversity_median=learn_div,
        diversity_delta_median=learn_div - heur_div,
        diversity_delta_mean=learn_div - heur_div - 0.04,
    )


class TestPrimitives:
    def test_fmt_handles_none(self) -> None:
        assert _fmt(None) == "—"
        assert _fmt(None, places=2) == "—"

    def test_fmt_ints_left_unformatted(self) -> None:
        assert _fmt(42) == "42"

    def test_fmt_floats_default_4dp(self) -> None:
        assert _fmt(0.12345) == "0.1235"

    def test_delta_none_propagates(self) -> None:
        assert _delta(None, 0.5) == "—"
        assert _delta(0.5, None) == "—"

    def test_delta_signs(self) -> None:
        assert _delta(0.10, 0.15).startswith("+")
        assert _delta(0.15, 0.10).startswith("-")

    def test_delta_respects_places(self) -> None:
        assert _delta(0.1, 0.2, places=2) == "+0.10"


class TestRenderMarkdown:
    def _mk(self, *, same_snapshot: bool = False) -> SnapshotComparison:
        a = _mk_snapshot("2026-04-scale-1k", corridors=898, mean_maps=74)
        b = _mk_snapshot(
            "2026-04-scale-1k" if same_snapshot else "2026-04-scale-3k-expansion",
            corridors=2100, mean_maps=420,
            heur_div=0.60, learn_div=0.57,
        )
        return SnapshotComparison(
            started_at=datetime.now(tz=timezone.utc),
            production_alpha=1.0,
            a=a, b=b,
        )

    def test_header_includes_both_snapshot_ids(self) -> None:
        md = render_markdown(self._mk())
        assert "2026-04-scale-1k" in md
        assert "2026-04-scale-3k-expansion" in md
        assert "Snapshot A/B Comparison" in md

    def test_corpus_table_shows_deltas(self) -> None:
        md = render_markdown(self._mk())
        # 2100 - 898 = +1202 corridors
        assert "+1202" in md
        # 420 - 74 = +346 mean-interval maps
        assert "+346" in md

    def test_scheme_table_renders_per_metric(self) -> None:
        md = render_markdown(self._mk())
        assert "time_envelope_v2_weighted" in md
        assert "label_stdev" in md
        assert "pred_stdev" in md
        assert "AUC (learned)" in md

    def test_diversity_delta_interpretation_lines(self) -> None:
        # B's delta_median should be LESS negative than A's → "less collapse"
        md = render_markdown(self._mk())
        # A: 0.54 - 0.58 = -0.04
        # B: 0.57 - 0.60 = -0.03
        # delta_delta = -0.03 - (-0.04) = +0.01 (below +0.02 threshold → no line)
        assert "collapses **less**" not in md and "collapses **more**" not in md

    def test_diversity_interpretation_triggered_on_meaningful_gap(self) -> None:
        # Force a large gap so the interpretation line fires.
        a = SnapshotSummary(
            snapshot_id="A",
            total_corridors=100, corridor_owning_maps=20,
            maps_with_mean_interval=10,
            schemes=[_mk_scheme("x")],
            diversity_intervals=10,
            heuristic_diversity_median=0.6,
            learned_diversity_median=0.5,
            diversity_delta_median=-0.1,  # A collapses a lot
            diversity_delta_mean=-0.08,
        )
        b = SnapshotSummary(
            snapshot_id="B",
            total_corridors=100, corridor_owning_maps=20,
            maps_with_mean_interval=10,
            schemes=[_mk_scheme("x")],
            diversity_intervals=10,
            heuristic_diversity_median=0.6,
            learned_diversity_median=0.60,
            diversity_delta_median=0.0,  # B: no collapse
            diversity_delta_mean=0.0,
        )
        c = SnapshotComparison(
            started_at=datetime.now(tz=timezone.utc),
            production_alpha=1.0, a=a, b=b,
        )
        md = render_markdown(c)
        # delta_delta = 0.0 - (-0.1) = +0.1, above +0.02 threshold → "less"
        assert "collapses **less**" in md

    def test_asymmetric_scheme_presence_handled(self) -> None:
        # Snapshot B has a scheme that A doesn't (e.g., new label only worked on B).
        a = _mk_snapshot("A", schemes=[_mk_scheme("time_envelope")])
        b = _mk_snapshot("B", schemes=[
            _mk_scheme("time_envelope"),
            _mk_scheme("time_envelope_v2_weighted"),
        ])
        c = SnapshotComparison(
            started_at=datetime.now(tz=timezone.utc),
            production_alpha=1.0, a=a, b=b,
        )
        md = render_markdown(c)
        # Both scheme names should appear; the absent side renders as "—".
        assert "time_envelope" in md
        assert "time_envelope_v2_weighted" in md
        assert "—" in md  # missing A entries for the B-only scheme


class TestBuildComparison:
    """The orchestrator validates inputs but is otherwise DB-dependent;
    we only check the pre-DB validation here."""

    def test_rejects_same_snapshot(self) -> None:
        from src.learning.compare_snapshots import build_comparison
        with pytest.raises(ValueError, match="must differ"):
            build_comparison(
                conn=None,  # type: ignore[arg-type]
                snapshot_a="x", snapshot_b="x",
            )
