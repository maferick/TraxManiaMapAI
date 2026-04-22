"""Side-by-side A/B comparison between two ingestion snapshots.

Wraps the existing ranking + diversity diagnostics with the
``--snapshot`` filter (landed in PR #29) and renders a single
markdown report with per-metric deltas between cohorts.

Use case: after a second ingestion snapshot lands alongside
``2026-04-scale-1k``, we want *one number per metric* showing how the
new cohort changes the picture, not two separate markdown files
the reader has to eyeball diff.

Anti-leakage: consumes only outputs of the existing diagnostics. No
new DB queries beyond what those already do. No rank-derived inputs
beyond what's already in the ranking pipeline.
"""
from __future__ import annotations

import io
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from pymysql.connections import Connection

from src.corridor.ranking.diagnose import (
    DEFAULT_ALPHAS,
    DiagnosticReport,
    run_diagnostics,
)
from src.diversity.metrics import DiversityReport, build_report, fetch_paths

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchemeSummary:
    """Condensed per-scheme metrics at the production α for the
    side-by-side view. Populated from a :class:`SchemeDiagnostic`
    inside a :class:`DiagnosticReport`."""
    label_scheme: str
    n_labeled: int
    label_stdev: float | None
    pred_stdev_at_prod_alpha: float | None
    test_rank_corr_at_prod_alpha: float | None
    auc_at_prod_alpha: float | None


@dataclass(frozen=True)
class SnapshotSummary:
    """Full snapshot snapshot (from ranking + diversity diagnostics)."""
    snapshot_id: str
    total_corridors: int
    corridor_owning_maps: int
    maps_with_mean_interval: int
    schemes: list[SchemeSummary] = field(default_factory=list)
    # Diversity watchdog slice — computed from fetch_paths + build_report.
    diversity_intervals: int = 0
    heuristic_diversity_median: float | None = None
    learned_diversity_median: float | None = None
    diversity_delta_median: float | None = None
    diversity_delta_mean: float | None = None
    # Raw underlying reports retained so future renderers can pull
    # fields we didn't surface today without re-running the pipeline.
    ranking_report: DiagnosticReport | None = None
    diversity_report: DiversityReport | None = None


@dataclass(frozen=True)
class SnapshotComparison:
    started_at: datetime
    production_alpha: float
    a: SnapshotSummary
    b: SnapshotSummary


def _scheme_summary(
    report: DiagnosticReport, production_alpha: float,
) -> list[SchemeSummary]:
    out: list[SchemeSummary] = []
    for s in report.schemes:
        label_stdev = s.label_summary.stdev if s.label_summary else None
        # Locate the sweep row at the production alpha. Equality may
        # not hit exactly for floats; tolerate a tiny epsilon.
        prod_row = None
        for row in s.sweep:
            if abs(row.alpha - production_alpha) < 1e-9:
                prod_row = row
                break
        out.append(SchemeSummary(
            label_scheme=s.label_scheme,
            n_labeled=s.n_labeled,
            label_stdev=label_stdev,
            pred_stdev_at_prod_alpha=(
                prod_row.pred_stdev_all if prod_row else None
            ),
            test_rank_corr_at_prod_alpha=(
                prod_row.test_rank_corr if prod_row else None
            ),
            auc_at_prod_alpha=(prod_row.auc_learned if prod_row else None),
        ))
    return out


def _build_one(
    conn: Connection,
    snapshot_id: str,
    *,
    alphas: Sequence[float],
    production_alpha: float,
) -> SnapshotSummary:
    """Run both diagnostics for one snapshot and condense. Heavy —
    each call materializes corridors + recomputes all four label
    schemes. Only use for A/B comparison work."""
    ranking = run_diagnostics(
        conn,
        alphas=alphas,
        production_alpha=production_alpha,
        snapshot_id=snapshot_id,
    )
    paths = fetch_paths(conn, snapshot_id=snapshot_id)
    diversity = build_report(paths, k=3)

    h = diversity.heuristic_summary
    l = diversity.learned_summary
    return SnapshotSummary(
        snapshot_id=snapshot_id,
        total_corridors=ranking.total_corridors,
        corridor_owning_maps=diversity.corridor_owning_maps,
        maps_with_mean_interval=ranking.maps_with_mean_interval,
        schemes=_scheme_summary(ranking, production_alpha),
        diversity_intervals=(l.intervals_compared if l else 0),
        heuristic_diversity_median=(
            h.diversity_median if h else None
        ),
        learned_diversity_median=(
            l.diversity_median if l else None
        ),
        diversity_delta_median=(
            (l.diversity_median - h.diversity_median)
            if (h is not None and l is not None) else None
        ),
        diversity_delta_mean=(
            (l.diversity_mean - h.diversity_mean)
            if (h is not None and l is not None) else None
        ),
        ranking_report=ranking,
        diversity_report=diversity,
    )


def build_comparison(
    conn: Connection,
    *,
    snapshot_a: str,
    snapshot_b: str,
    alphas: Sequence[float] = DEFAULT_ALPHAS,
    production_alpha: float = 1.0,
) -> SnapshotComparison:
    if snapshot_a == snapshot_b:
        raise ValueError("snapshot_a and snapshot_b must differ")
    _LOG.info("running A-side diagnostics for snapshot=%s", snapshot_a)
    a = _build_one(
        conn, snapshot_a,
        alphas=alphas, production_alpha=production_alpha,
    )
    _LOG.info("running B-side diagnostics for snapshot=%s", snapshot_b)
    b = _build_one(
        conn, snapshot_b,
        alphas=alphas, production_alpha=production_alpha,
    )
    return SnapshotComparison(
        started_at=datetime.now(tz=timezone.utc),
        production_alpha=production_alpha,
        a=a, b=b,
    )


# ---------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------

def _fmt(v: float | int | None, places: int = 4, sign: bool = False) -> str:
    if v is None:
        return "—"
    if isinstance(v, int):
        return str(v)
    if sign:
        return f"{v:+.{places}f}"
    return f"{v:.{places}f}"


def _delta(a: float | None, b: float | None, places: int = 4) -> str:
    if a is None or b is None:
        return "—"
    return f"{(b - a):+.{places}f}"


def _corpus_table(
    buf: io.StringIO, c: SnapshotComparison,
) -> None:
    buf.write("## Corpus size\n\n")
    buf.write(
        f"| metric | A (`{c.a.snapshot_id}`) | B (`{c.b.snapshot_id}`) | Δ (B−A) |\n"
        "|---|---|---|---|\n"
    )
    for name, a_v, b_v in (
        ("corridors", c.a.total_corridors, c.b.total_corridors),
        ("corridor-owning maps", c.a.corridor_owning_maps, c.b.corridor_owning_maps),
        ("maps with mean interval (labeled pool)",
         c.a.maps_with_mean_interval, c.b.maps_with_mean_interval),
    ):
        delta = b_v - a_v
        buf.write(
            f"| {name} | {a_v} | {b_v} | {delta:+d} |\n"
        )
    buf.write("\n")


def _scheme_delta_table(
    buf: io.StringIO, c: SnapshotComparison,
) -> None:
    buf.write(f"## Ranking schemes @ α = {c.production_alpha}\n\n")
    # Pair schemes by label name; absent from either side → "—".
    a_by = {s.label_scheme: s for s in c.a.schemes}
    b_by = {s.label_scheme: s for s in c.b.schemes}
    all_schemes = sorted(set(a_by.keys()) | set(b_by.keys()))
    buf.write(
        "| scheme | metric | A | B | Δ |\n"
        "|---|---|---|---|---|\n"
    )
    for scheme in all_schemes:
        sa = a_by.get(scheme)
        sb = b_by.get(scheme)
        rows = (
            ("n_labeled",
             sa.n_labeled if sa else None,
             sb.n_labeled if sb else None),
            ("label_stdev",
             sa.label_stdev if sa else None,
             sb.label_stdev if sb else None),
            ("pred_stdev",
             sa.pred_stdev_at_prod_alpha if sa else None,
             sb.pred_stdev_at_prod_alpha if sb else None),
            ("test_rank_corr",
             sa.test_rank_corr_at_prod_alpha if sa else None,
             sb.test_rank_corr_at_prod_alpha if sb else None),
            ("AUC (learned)",
             sa.auc_at_prod_alpha if sa else None,
             sb.auc_at_prod_alpha if sb else None),
        )
        for label, a_v, b_v in rows:
            places = 0 if label == "n_labeled" else 4
            if label == "n_labeled":
                delta_str = (
                    _delta(a_v, b_v, places=0) if a_v is not None and b_v is not None
                    else "—"
                )
            else:
                delta_str = _delta(a_v, b_v, places=places)
            buf.write(
                f"| `{scheme}` | {label} | "
                f"{_fmt(a_v, places=places)} | "
                f"{_fmt(b_v, places=places)} | "
                f"{delta_str} |\n"
            )
    buf.write("\n")


def _diversity_table(
    buf: io.StringIO, c: SnapshotComparison,
) -> None:
    buf.write("## Diversity watchdog (A3)\n\n")
    buf.write(
        "| metric | A | B | Δ |\n"
        "|---|---|---|---|\n"
    )
    rows = (
        ("intervals compared",
         c.a.diversity_intervals, c.b.diversity_intervals),
        ("heuristic diversity (median)",
         c.a.heuristic_diversity_median, c.b.heuristic_diversity_median),
        ("learned diversity (median)",
         c.a.learned_diversity_median, c.b.learned_diversity_median),
        ("Δ (learned − heuristic) median",
         c.a.diversity_delta_median, c.b.diversity_delta_median),
        ("Δ (learned − heuristic) mean",
         c.a.diversity_delta_mean, c.b.diversity_delta_mean),
    )
    for name, a_v, b_v in rows:
        if isinstance(a_v, int) and isinstance(b_v, int):
            delta = f"{b_v - a_v:+d}"
            buf.write(
                f"| {name} | {a_v} | {b_v} | {delta} |\n"
            )
        else:
            buf.write(
                f"| {name} | {_fmt(a_v)} | {_fmt(b_v)} | {_delta(a_v, b_v)} |\n"
            )
    buf.write("\n")
    # Plain-language hint when the deltas suggest something meaningful.
    if (
        c.a.diversity_delta_median is not None
        and c.b.diversity_delta_median is not None
    ):
        delta_delta = c.b.diversity_delta_median - c.a.diversity_delta_median
        if delta_delta > 0.02:
            buf.write(
                f"_B's learned ranker collapses **less** than A's "
                f"(median diversity gap {delta_delta:+.3f})._\n\n"
            )
        elif delta_delta < -0.02:
            buf.write(
                f"_B's learned ranker collapses **more** than A's "
                f"(median diversity gap {delta_delta:+.3f})._\n\n"
            )


def _header(buf: io.StringIO, c: SnapshotComparison) -> None:
    buf.write("# Snapshot A/B Comparison\n\n")
    buf.write(f"- **Generated**: `{c.started_at.isoformat()}`\n")
    buf.write(f"- **Snapshot A**: `{c.a.snapshot_id}`\n")
    buf.write(f"- **Snapshot B**: `{c.b.snapshot_id}`\n")
    buf.write(f"- **Production α**: {c.production_alpha}\n\n")
    buf.write(
        "Δ columns are **B − A** throughout. Positive Δ means B is "
        "larger / higher than A for that metric; negative means B "
        "is smaller / lower.\n\n"
    )


def _footer(buf: io.StringIO) -> None:
    buf.write("## Notes\n\n")
    buf.write(
        "- Rank-corr / AUC use the same deterministic train/test split "
        "(seed=42) applied independently within each snapshot.\n"
    )
    buf.write(
        "- Proxy-cohort membership for AUC is snapshot-agnostic (pulled "
        "from `data/benchmarks/tech-*-proxy*.yaml`). If a map in A or B "
        "isn't in either proxy cohort, it doesn't contribute to the "
        "AUC computation — so the AUC value is directly comparable.\n"
    )
    buf.write(
        "- Diversity metric uses Jaccard on `path_cells` — raw geometry, "
        "no rank or score input.\n"
    )


def render_markdown(c: SnapshotComparison) -> str:
    buf = io.StringIO()
    _header(buf, c)
    _corpus_table(buf, c)
    _scheme_delta_table(buf, c)
    _diversity_table(buf, c)
    _footer(buf)
    return buf.getvalue()
