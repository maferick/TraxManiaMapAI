"""Markdown renderer for :class:`DryRunReport`.

Report sections match the PR 7 roadmap:
- setup
- score distributions per (evaluator, score dimension)
- benchmark-set rankings
- known-strong vs known-mediocre separation
- evaluator-vs-benchmark disagreements
- cross-evaluator disagreements

Kept deliberately textual — no plots, no external deps. A reader
should be able to diff two report versions meaningfully in plain git.
"""
from __future__ import annotations

import io
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from src.evaluation.dryrun.runner import DryRunMap, DryRunReport
from src.evaluation.dryrun.stats import (
    disagreement_pairs,
    histogram,
    quartiles,
    rank_correlation,
    separation_auc,
)


_SCORE_FIELDS: tuple[str, ...] = (
    "structural_score",
    "drivability_score",
    "flow_score",
    "style_score",
    "novelty_score",
)

_STRONG_CATEGORY = "strong_tech"
_MEDIOCRE_CATEGORY = "mediocre_tech"
_NEGATIVE_ROLE = "negative"
_DISAGREEMENT_THRESHOLD = 0.2


@dataclass(frozen=True)
class _ScoresByMap:
    evaluator_id: str              # "<name>@<version>"
    score_field: str
    values: dict[int, float]       # map_id -> score


def _collect_scores(report: DryRunReport) -> list[_ScoresByMap]:
    by_key: dict[tuple[str, str, str], dict[int, float]] = defaultdict(dict)
    for map_id, results in report.results.items():
        for r in results:
            for field in _SCORE_FIELDS:
                val = getattr(r, field, None)
                if val is None:
                    continue
                by_key[(r.evaluator_name, r.evaluator_version, field)][map_id] = float(val)
    return [
        _ScoresByMap(
            evaluator_id=f"{name}@{version}",
            score_field=field,
            values=values,
        )
        for (name, version, field), values in sorted(by_key.items())
    ]


def _maps_by_category(report: DryRunReport, category: str) -> list[DryRunMap]:
    return [
        m
        for m in report.maps
        if any(b.category == category for b in m.memberships)
    ]


def _write_header(buf: io.StringIO, report: DryRunReport) -> None:
    completed = report.completed_at.isoformat() if report.completed_at else "in-progress"
    duration_ms = (
        int((report.completed_at - report.started_at).total_seconds() * 1000)
        if report.completed_at
        else None
    )
    buf.write("# Evaluator Dry-Run Report v1\n\n")
    buf.write(f"- **Run id**: `{report.run_id}`\n")
    buf.write(f"- **Started at**: `{report.started_at.isoformat()}`\n")
    buf.write(f"- **Completed at**: `{completed}`\n")
    if duration_ms is not None:
        buf.write(f"- **Duration**: {duration_ms} ms\n")
    buf.write(f"- **Stage version**: `{report.stage_version}`\n")
    buf.write(f"- **Evaluators**: {', '.join(f'`{e}`' for e in report.evaluator_ids) or '_(none)_'}\n")
    bench_list = ", ".join(f"`{b}`" for b in report.benchmark_versions)
    buf.write(f"- **Benchmark versions**: {bench_list or '_(none)_'}\n")
    buf.write("\n")


def _write_overview(buf: io.StringIO, report: DryRunReport) -> None:
    buf.write("## Overview\n\n")
    total = len(report.maps)
    in_bench = sum(1 for m in report.maps if m.in_any_benchmark)
    community = total - in_bench
    buf.write(f"- Maps evaluated: **{total}**\n")
    buf.write(f"- In benchmark sets: **{in_bench}**\n")
    buf.write(f"- Community sample: **{community}**\n")
    buf.write(f"- Total results: **{sum(len(v) for v in report.results.values())}**\n")
    if report.errors:
        buf.write(f"- **Errors**: {len(report.errors)} (see end of report)\n")
    buf.write("\n")


def _write_distributions(
    buf: io.StringIO, scores: list[_ScoresByMap]
) -> None:
    buf.write("## Score distributions\n\n")
    if not scores:
        buf.write("_No score values produced._\n\n")
        return
    buf.write("| Evaluator | Dimension | N | min | Q1 | median | Q3 | max | mean |\n")
    buf.write("|---|---|---|---|---|---|---|---|---|\n")
    for s in scores:
        q = quartiles(list(s.values.values()))
        if q is None:
            continue
        buf.write(
            f"| `{s.evaluator_id}` | `{s.score_field}` | "
            + " | ".join(q.as_row())
            + " |\n"
        )
    buf.write("\n")
    for s in scores:
        if not s.values:
            continue
        buf.write(f"### `{s.evaluator_id}` / `{s.score_field}`\n\n")
        h = histogram(list(s.values.values()), bins=10)
        buf.write("```\n")
        for line in h.ascii_bar():
            buf.write(line + "\n")
        buf.write("```\n\n")


def _write_benchmark_rankings(
    buf: io.StringIO, report: DryRunReport, scores: list[_ScoresByMap]
) -> None:
    buf.write("## Benchmark-set rankings\n\n")
    if not report.benchmark_versions:
        buf.write("_No benchmark sets were run._\n\n")
        return
    for bench_version in report.benchmark_versions:
        bench_maps = report.maps_by_membership(bench_version)
        buf.write(f"### `{bench_version}` ({len(bench_maps)} maps)\n\n")
        if not bench_maps:
            buf.write("_No maps resolved for this set (all entries missing from the pinned snapshot)._\n\n")
            continue
        bench_ids = {m.map_id for m in bench_maps}
        for s in scores:
            subset = [(mid, v) for mid, v in s.values.items() if mid in bench_ids]
            if not subset:
                continue
            subset.sort(key=lambda kv: kv[1], reverse=True)
            buf.write(
                f"#### `{s.evaluator_id}` / `{s.score_field}` — top results\n\n"
            )
            buf.write("| rank | map_id | score | role |\n|---|---|---|---|\n")
            role_by_id = {}
            for m in bench_maps:
                for b in m.memberships:
                    if b.benchmark_version == bench_version:
                        role_by_id[m.map_id] = b.role
                        break
            for rank, (mid, score) in enumerate(subset[:10], start=1):
                buf.write(
                    f"| {rank} | {mid} | {score:+.4f} | "
                    f"{role_by_id.get(mid, '_n/a_')} |\n"
                )
            buf.write("\n")


def _write_separation(
    buf: io.StringIO, report: DryRunReport, scores: list[_ScoresByMap]
) -> None:
    buf.write("## Known-strong vs known-mediocre separation\n\n")
    strong_ids = {m.map_id for m in _maps_by_category(report, _STRONG_CATEGORY)}
    mediocre_ids = {m.map_id for m in _maps_by_category(report, _MEDIOCRE_CATEGORY)}
    if not strong_ids and not mediocre_ids:
        buf.write(
            "_No maps in categories `strong_tech` / `mediocre_tech` — "
            "separation not computable._\n\n"
        )
        return
    buf.write(
        f"Positives: {len(strong_ids)} strong-tech map(s). "
        f"Negatives: {len(mediocre_ids)} mediocre-tech map(s).\n\n"
    )
    if not strong_ids or not mediocre_ids:
        buf.write(
            "_Only one side present; skipping AUC. Add maps to the other "
            "category in a follow-up benchmark version._\n\n"
        )
        return
    buf.write("| Evaluator | Dimension | AUC (positives vs negatives) |\n")
    buf.write("|---|---|---|\n")
    for s in scores:
        pos = [s.values[m] for m in strong_ids if m in s.values]
        neg = [s.values[m] for m in mediocre_ids if m in s.values]
        auc = separation_auc(pos, neg)
        buf.write(
            f"| `{s.evaluator_id}` | `{s.score_field}` | "
            + (f"{auc:.4f}" if auc is not None else "_n/a_")
            + " |\n"
        )
    buf.write("\n_AUC = 0.5 is no separation; 1.0 is perfect; "
              "< 0.5 means negatives score higher than positives "
              "(an evaluator that's inverted)._\n\n")


def _write_evaluator_vs_benchmark_disagreements(
    buf: io.StringIO, report: DryRunReport, scores: list[_ScoresByMap]
) -> None:
    buf.write("## Evaluator-vs-benchmark disagreements\n\n")
    rows: list[tuple[str, str, int, float, str, str]] = []
    for s in scores:
        for m in report.maps:
            if not m.in_any_benchmark:
                continue
            val = s.values.get(m.map_id)
            if val is None:
                continue
            for b in m.memberships:
                if b.role == _NEGATIVE_ROLE and val >= 1.0 - _DISAGREEMENT_THRESHOLD:
                    rows.append(
                        (s.evaluator_id, s.score_field, m.map_id, val, b.role, b.benchmark_version)
                    )
                elif (
                    b.role in ("primary", "reference")
                    and val <= _DISAGREEMENT_THRESHOLD
                ):
                    rows.append(
                        (s.evaluator_id, s.score_field, m.map_id, val, b.role, b.benchmark_version)
                    )
    if not rows:
        buf.write("_No disagreements above the "
                  f"{_DISAGREEMENT_THRESHOLD:.2f} threshold._\n\n")
        return
    buf.write("| evaluator | dimension | map_id | score | role | benchmark |\n")
    buf.write("|---|---|---|---|---|---|\n")
    for row in rows:
        evalr, field, mid, val, role, bench = row
        buf.write(
            f"| `{evalr}` | `{field}` | {mid} | {val:+.4f} | "
            f"{role} | `{bench}` |\n"
        )
    buf.write("\n")


def _write_cross_evaluator_disagreements(
    buf: io.StringIO, scores: list[_ScoresByMap]
) -> None:
    buf.write("## Cross-evaluator disagreements\n\n")
    # Compare pairs of (evaluator, field) that share a score dimension.
    by_field: dict[str, list[_ScoresByMap]] = defaultdict(list)
    for s in scores:
        by_field[s.score_field].append(s)
    any_pairs = False
    for field, entries in sorted(by_field.items()):
        if len(entries) < 2:
            continue
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                a, b = entries[i], entries[j]
                pairs = disagreement_pairs(
                    a.values, b.values, threshold=_DISAGREEMENT_THRESHOLD
                )
                if not pairs:
                    continue
                any_pairs = True
                buf.write(
                    f"### `{a.evaluator_id}` vs `{b.evaluator_id}` on `{field}`\n\n"
                )
                buf.write(
                    "| map_id | "
                    f"`{a.evaluator_id}` | `{b.evaluator_id}` | |Δ| |\n"
                    "|---|---|---|---|\n"
                )
                for mid, av, bv in pairs:
                    buf.write(f"| {mid} | {av:+.4f} | {bv:+.4f} | {abs(av-bv):.4f} |\n")
                buf.write("\n")
    if not any_pairs:
        buf.write(
            "_No cross-evaluator disagreements above the "
            f"{_DISAGREEMENT_THRESHOLD:.2f} threshold._\n\n"
        )


def _write_ranking_stability(
    buf: io.StringIO, scores: list[_ScoresByMap]
) -> None:
    """Spearman rank correlation + stdev ratio for every pair of
    evaluators that share a score dimension.

    Answers the question "does the learned evaluator agree with the
    heuristic on rank, without collapsing the distribution?" High
    correlation + comparable stdev = improved ranking without diversity
    loss. High correlation + much lower stdev = diversity collapse —
    the learned model agrees where the heuristic was confident but
    flattens signal elsewhere. Low correlation = genuine disagreement
    (follow-up analysis territory).
    """
    buf.write("## Ranking stability + diversity\n\n")
    by_field: dict[str, list[_ScoresByMap]] = defaultdict(list)
    for s in scores:
        by_field[s.score_field].append(s)

    any_rows = False
    for field, entries in sorted(by_field.items()):
        if len(entries) < 2:
            continue
        any_rows = True
        buf.write(f"### `{field}`\n\n")
        buf.write(
            "| evaluator A | evaluator B | shared maps | "
            "rank corr | stdev(A) | stdev(B) | stdev ratio (B/A) |\n"
            "|---|---|---|---|---|---|---|\n"
        )
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                a, b = entries[i], entries[j]
                shared = a.values.keys() & b.values.keys()
                corr = rank_correlation(a.values, b.values)
                a_vals = list(a.values.values())
                b_vals = list(b.values.values())
                import statistics as _stats
                a_std = _stats.stdev(a_vals) if len(a_vals) >= 2 else 0.0
                b_std = _stats.stdev(b_vals) if len(b_vals) >= 2 else 0.0
                ratio = (b_std / a_std) if a_std > 0 else float("inf")
                corr_str = f"{corr:+.4f}" if corr is not None else "n/a"
                buf.write(
                    f"| `{a.evaluator_id}` | `{b.evaluator_id}` | "
                    f"{len(shared)} | {corr_str} | {a_std:.4f} | {b_std:.4f} | "
                    f"{ratio:.3f} |\n"
                )
        buf.write("\n")
    if not any_rows:
        buf.write("_Only one evaluator per score dimension; no pairs to compare._\n\n")


def _write_errors(buf: io.StringIO, report: DryRunReport) -> None:
    if not report.errors:
        return
    buf.write("## Errors\n\n")
    for msg in report.errors:
        buf.write(f"- `{msg}`\n")
    buf.write("\n")


def render_markdown(report: DryRunReport) -> str:
    buf = io.StringIO()
    _write_header(buf, report)
    _write_overview(buf, report)
    scores = _collect_scores(report)
    _write_distributions(buf, scores)
    _write_benchmark_rankings(buf, report, scores)
    _write_separation(buf, report, scores)
    _write_evaluator_vs_benchmark_disagreements(buf, report, scores)
    _write_cross_evaluator_disagreements(buf, scores)
    _write_ranking_stability(buf, scores)
    _write_errors(buf, report)
    return buf.getvalue()
