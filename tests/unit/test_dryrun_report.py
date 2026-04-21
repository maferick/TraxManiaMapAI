from __future__ import annotations

from datetime import datetime, timezone

from src.evaluation.base import EvaluationResult, utcnow
from src.evaluation.dryrun import BenchmarkMembership, DryRunMap, DryRunReport
from src.evaluation.dryrun.report import render_markdown


def _make_result(
    map_id: int,
    *,
    evaluator_name: str = "structural",
    structural_score: float | None = None,
    drivability_score: float | None = None,
    benchmark: str | None = None,
) -> EvaluationResult:
    return EvaluationResult(
        map_id=map_id,
        evaluator_name=evaluator_name,
        evaluator_version="0.1.0",
        benchmark_set_version=benchmark,
        created_at=utcnow(),
        code_version="abc",
        source_artifact_ids={"map": str(map_id)},
        structural_score=structural_score,
        drivability_score=drivability_score,
    )


def _make_report() -> DryRunReport:
    strong = DryRunMap(
        map_id=1,
        source_map_id="strong1",
        ingestion_snapshot="2026-04-test",
        memberships=(
            BenchmarkMembership(
                benchmark_version="tech-strong-v1",
                category="strong_tech",
                role="primary",
                label={},
            ),
        ),
    )
    mediocre = DryRunMap(
        map_id=2,
        source_map_id="med1",
        ingestion_snapshot="2026-04-test",
        memberships=(
            BenchmarkMembership(
                benchmark_version="tech-mediocre-v1",
                category="mediocre_tech",
                role="primary",
                label={},
            ),
        ),
    )
    community = DryRunMap(map_id=3, source_map_id="comm", ingestion_snapshot="2026-04-test")

    report = DryRunReport(
        run_id="testrun",
        started_at=datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc),
        stage_version="0.1.0",
        evaluator_ids=("structural@0.1.0", "adjacency_graph@0.1.0"),
        benchmark_versions=("tech-strong-v1", "tech-mediocre-v1"),
        maps=[strong, mediocre, community],
        results={
            1: [
                _make_result(1, structural_score=0.95, benchmark="tech-strong-v1"),
                _make_result(1, evaluator_name="adjacency_graph", structural_score=0.40),
            ],
            2: [
                _make_result(2, structural_score=0.55, benchmark="tech-mediocre-v1"),
                _make_result(2, evaluator_name="adjacency_graph", structural_score=0.55),
            ],
            3: [_make_result(3, structural_score=0.70)],
        },
        completed_at=datetime(2026, 4, 21, 12, 0, 5, tzinfo=timezone.utc),
    )
    return report


def test_markdown_contains_required_sections() -> None:
    md = render_markdown(_make_report())
    for header in (
        "# Evaluator Dry-Run Report v1",
        "## Overview",
        "## Score distributions",
        "## Benchmark-set rankings",
        "## Known-strong vs known-mediocre separation",
        "## Evaluator-vs-benchmark disagreements",
        "## Cross-evaluator disagreements",
    ):
        assert header in md, f"missing section: {header}"


def test_markdown_header_pins_versions() -> None:
    md = render_markdown(_make_report())
    assert "`testrun`" in md
    assert "`structural@0.1.0`" in md
    assert "`adjacency_graph@0.1.0`" in md
    assert "`tech-strong-v1`" in md
    assert "`tech-mediocre-v1`" in md


def test_separation_renders_auc_for_matching_categories() -> None:
    md = render_markdown(_make_report())
    assert "AUC" in md
    # Strong gets structural 0.95, mediocre gets 0.55 → positives > negatives → AUC = 1.0
    assert "1.0000" in md


def test_cross_evaluator_disagreement_detected() -> None:
    # Strong map: structural=0.95, adjacency_graph=0.40 → |Δ| = 0.55 ≥ 0.2
    md = render_markdown(_make_report())
    assert "Cross-evaluator disagreements" in md
    assert "structural@0.1.0" in md and "adjacency_graph@0.1.0" in md


def test_zero_maps_produces_valid_markdown() -> None:
    report = DryRunReport(
        run_id="empty",
        started_at=datetime(2026, 4, 21, 0, 0, 0, tzinfo=timezone.utc),
        stage_version="0.1.0",
        evaluator_ids=(),
        benchmark_versions=(),
        completed_at=datetime(2026, 4, 21, 0, 0, 1, tzinfo=timezone.utc),
    )
    md = render_markdown(report)
    assert "# Evaluator Dry-Run Report v1" in md
    assert "Maps evaluated: **0**" in md


def test_empty_category_side_skips_auc() -> None:
    # A report with only strong_tech (no mediocre_tech) should skip AUC gracefully.
    strong = DryRunMap(
        map_id=1,
        source_map_id="s",
        ingestion_snapshot="snap",
        memberships=(
            BenchmarkMembership(
                benchmark_version="tech-strong-v1",
                category="strong_tech",
                role="primary",
                label={},
            ),
        ),
    )
    report = DryRunReport(
        run_id="only-strong",
        started_at=datetime(2026, 4, 21, 0, 0, 0, tzinfo=timezone.utc),
        stage_version="0.1.0",
        evaluator_ids=("structural@0.1.0",),
        benchmark_versions=("tech-strong-v1",),
        maps=[strong],
        results={1: [_make_result(1, structural_score=0.9)]},
        completed_at=datetime(2026, 4, 21, 0, 0, 1, tzinfo=timezone.utc),
    )
    md = render_markdown(report)
    assert "Only one side present" in md
