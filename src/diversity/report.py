"""Markdown renderer for :class:`DiversityReport`.

Textual sections only; no plots. A reader should be able to diff
two reports in plain git.
"""
from __future__ import annotations

import io
import statistics
from datetime import datetime, timezone

from src.diversity.metrics import DiversityReport, IntervalDiversity, RankerDiversitySummary


def _quartiles(values: list[float]) -> tuple[float, float, float, float, float]:
    """(min, q1, median, q3, max). Safe on empty."""
    if not values:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    s = sorted(values)
    n = len(s)
    def _q(p: float) -> float:
        return s[max(0, min(n - 1, int(p * (n - 1))))]
    return s[0], _q(0.25), _q(0.5), _q(0.75), s[-1]


def _header(buf: io.StringIO, report: DiversityReport) -> None:
    buf.write("# Corridor Diversity Diagnostic\n\n")
    buf.write(f"- **Generated**: `{datetime.now(tz=timezone.utc).isoformat()}`\n")
    buf.write(f"- **Total corridors**: {report.total_corridors}\n")
    buf.write(f"- **Corridor-owning maps**: {report.corridor_owning_maps}\n")
    buf.write(f"- **Top-K for pairwise**: {report.top_k}\n")
    buf.write("\n")
    buf.write(
        "Similarity metric: **Jaccard on `path_cells`** — raw cell "
        "overlap. No rank-derived, no learned-score-derived inputs.\n\n"
    )
    buf.write(
        "Higher mean_pairwise_similarity → top-K corridors share "
        "more cells → less diversity. A ranker that collapses variety "
        "will show high similarity in its top-K picks.\n\n"
    )


def _write_interval_distribution(
    buf: io.StringIO, report: DiversityReport,
) -> None:
    buf.write("## Within-interval diversity (native path_rank ordering)\n\n")
    if not report.intervals:
        buf.write("_No intervals with ≥ 2 corridors — metric inapplicable._\n\n")
        return
    sims = [i.mean_pairwise_similarity for i in report.intervals]
    divs = [i.diversity for i in report.intervals]
    lo, q1, med, q3, hi = _quartiles(sims)
    lo_d, q1_d, med_d, q3_d, hi_d = _quartiles(divs)
    buf.write(f"_{len(report.intervals)} intervals had ≥ 2 corridors._\n\n")
    buf.write(
        "| stat | mean_pairwise_sim | diversity (1-sim) |\n"
        "|---|---|---|\n"
        f"| min | {lo:.4f} | {lo_d:.4f} |\n"
        f"| Q1 | {q1:.4f} | {q1_d:.4f} |\n"
        f"| median | {med:.4f} | {med_d:.4f} |\n"
        f"| Q3 | {q3:.4f} | {q3_d:.4f} |\n"
        f"| max | {hi:.4f} | {hi_d:.4f} |\n"
        f"| mean | {statistics.mean(sims):.4f} | {statistics.mean(divs):.4f} |\n\n"
    )
    # Worst 5 (most collapsed) intervals.
    worst = sorted(
        report.intervals,
        key=lambda i: i.mean_pairwise_similarity, reverse=True,
    )[:5]
    buf.write("### Most collapsed intervals (highest top-K similarity)\n\n")
    buf.write(
        "| map_id | src | dst | corridors | top_k | similarity | diversity |\n"
        "|---|---|---|---|---|---|---|\n"
    )
    for w in worst:
        buf.write(
            f"| {w.map_id} | {w.src_tag}#{w.src_order} | "
            f"{w.dst_tag}#{w.dst_order} | {w.corridor_count} | "
            f"{w.top_k} | {w.mean_pairwise_similarity:.4f} | "
            f"{w.diversity:.4f} |\n"
        )
    buf.write("\n")


def _write_cross_map(buf: io.StringIO, report: DiversityReport) -> None:
    q = report.rank0_cross_map_similarity_quartiles
    buf.write("## Top-rank cross-map overlap\n\n")
    if q["n_pairs"] == 0:
        buf.write("_< 2 rank-0 corridors — distribution undefined._\n\n")
        return
    buf.write(f"Sampled {int(q['n_pairs'])} random pairs of rank-0 corridors.\n\n")
    buf.write(
        "| n_pairs | Q1 | median | Q3 | mean |\n"
        "|---|---|---|---|---|\n"
        f"| {int(q['n_pairs'])} | {q['q1']:.4f} | {q['median']:.4f} | "
        f"{q['q3']:.4f} | {q['mean']:.4f} |\n\n"
    )
    buf.write(
        "_Different maps have different geometries → low overlap is "
        "normal. High numbers here would flag a degenerate ranker "
        "(e.g. all top-ranks are short straight lines)._\n\n"
    )


def _write_virtual_and_length(
    buf: io.StringIO, report: DiversityReport,
) -> None:
    buf.write("## Top-rank virtual-edge + length concentration\n\n")
    buf.write(
        f"- **Virtual-edge fraction (rank 0)**: "
        f"{report.virtual_edge_fraction_top_rank:.2%}\n"
    )
    buf.write(
        f"- **Path length median (rank 0)**: "
        f"{report.path_length_median_top_rank:.1f} cells\n"
    )
    buf.write(
        f"- **Path length stdev (rank 0)**: "
        f"{report.path_length_stdev_top_rank:.2f} cells\n\n"
    )
    buf.write(
        "_Either extreme of virtual-edge fraction (≈0% or ≈100%) "
        "is interesting: 0% means replay signal isn't reaching the "
        "ranker at all; 100% means the ranker over-relies on replay "
        "bridges. Path length stdev near 0 means the ranker prefers "
        "one canonical length._\n\n"
    )


def _write_ranker_summary(buf: io.StringIO, summary: RankerDiversitySummary) -> None:
    buf.write(
        f"### `{summary.ranker}` — {summary.intervals_compared} intervals compared\n\n"
    )
    buf.write(
        f"- mean_pairwise_similarity: median={summary.mean_pairwise_similarity_median:.4f}, "
        f"mean={summary.mean_pairwise_similarity_mean:.4f}\n"
    )
    buf.write(
        f"- diversity (1-sim): median={summary.diversity_median:.4f}, "
        f"mean={summary.diversity_mean:.4f}\n\n"
    )
    if summary.worst_intervals:
        buf.write(
            "Worst (most collapsed by this ranker):\n\n"
            "| map_id | src | dst | corridors | top_k | similarity |\n"
            "|---|---|---|---|---|---|\n"
        )
        for w in summary.worst_intervals:
            buf.write(
                f"| {w.map_id} | {w.src_tag}#{w.src_order} | "
                f"{w.dst_tag}#{w.dst_order} | {w.corridor_count} | "
                f"{w.top_k} | {w.mean_pairwise_similarity:.4f} |\n"
            )
        buf.write("\n")


def _write_cross_ranker(buf: io.StringIO, report: DiversityReport) -> None:
    h = report.heuristic_summary
    l = report.learned_summary
    if h is None and l is None:
        return
    buf.write("## Ranker comparison — heuristic vs learned\n\n")
    buf.write(
        "For each interval with ≥ 2 corridors, re-order by the ranker's "
        "score (descending), then compute mean pairwise Jaccard over the "
        "top-K picks. Compare the distributions.\n\n"
    )
    if h is not None:
        _write_ranker_summary(buf, h)
    if l is not None:
        _write_ranker_summary(buf, l)
    if h is not None and l is not None:
        delta_sim = (
            l.mean_pairwise_similarity_mean - h.mean_pairwise_similarity_mean
        )
        delta_div = l.diversity_mean - h.diversity_mean
        buf.write("### Delta\n\n")
        buf.write(
            f"- mean_pairwise_similarity: learned − heuristic = "
            f"{delta_sim:+.4f}\n"
            f"  ({'↑ more collapse' if delta_sim > 0 else '↓ less collapse'})\n"
        )
        buf.write(
            f"- diversity: learned − heuristic = {delta_div:+.4f}\n"
            f"  ({'↓ worse diversity' if delta_div < 0 else '↑ better diversity'})\n\n"
        )


def _footer(buf: io.StringIO) -> None:
    buf.write("## Notes\n\n")
    buf.write(
        "- Similarity uses only raw `path_cells`. No feature, no score, "
        "no rank-derived input enters the metric itself.\n"
    )
    buf.write(
        "- Interpretation is the reader's job. One interval with high "
        "similarity on a map that has only one sensible path is not "
        "collapse; many such intervals together might be.\n"
    )
    buf.write(
        "- See `docs/learning/corridor-diversity-metrics.md` for the "
        "design, non-goals, and anti-leakage rules.\n"
    )


def render_markdown(report: DiversityReport) -> str:
    buf = io.StringIO()
    _header(buf, report)
    _write_interval_distribution(buf, report)
    _write_cross_map(buf, report)
    _write_virtual_and_length(buf, report)
    _write_cross_ranker(buf, report)
    _footer(buf)
    return buf.getvalue()
