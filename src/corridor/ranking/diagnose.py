"""Orchestrator + markdown renderer for the corridor-ranking score
spread diagnostic.

Wires :mod:`src.corridor.ranking.diagnostics` to real DB-materialized
features + labels (both v0.1 inverse_rank and v0.2 time_envelope)
and emits a single markdown report the reader can skim to decide
whether the score compression is label-bound, regularization-bound,
or feature-bound.

Read-only. Does not persist any model or DB rows.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import numpy as np
from pymysql.connections import Connection

from src.corridor.ranking.diagnostics import (
    FeatureAblationRow,
    LabelSpreadSummary,
    RegularizationSweepRow,
    feature_ablation,
    label_distribution_summary,
    regularization_sweep,
)
from src.corridor.ranking.features import (
    FEATURE_NAMES,
    build_feature_matrix,
    load_corridor_rows,
)
from src.corridor.ranking.labels import synthesize_inverse_rank_labels
from src.corridor.ranking.time_envelope_labels import (
    _load_map_mean_interval_ms,
    synthesize_time_envelope_labels,
)
from src.corridor.ranking.time_envelope_labels_v2 import (
    load_map_interval_stats,
    synthesize_time_envelope_v2_labels,
)
from src.corridor.ranking.train import _fetch_cohort_map_ids
from src.corridor.traversability.classification import CLASSIFICATION_VERSION

_LOG = logging.getLogger(__name__)


# Alphas chosen to span five decades around the production α=1.0 so
# the stdev curve is visible across the range where ridge actually
# changes behavior.
DEFAULT_ALPHAS: tuple[float, ...] = (0.001, 0.01, 0.1, 1.0, 10.0, 100.0)


@dataclass
class SchemeDiagnostic:
    label_scheme: str
    label_summary: LabelSpreadSummary | None
    sweep: list[RegularizationSweepRow]
    ablation_baseline: RegularizationSweepRow | None
    ablation: list[FeatureAblationRow]
    n_labeled: int


@dataclass
class DiagnosticReport:
    started_at: datetime
    total_corridors: int
    maps_with_mean_interval: int
    alphas: tuple[float, ...]
    production_alpha: float
    schemes: list[SchemeDiagnostic]
    # v2-only extras — populated when time_envelope_v2 ran
    v2_aggregation_method: str | None = None
    v2_map_count: int = 0
    v2_label_quality_summary: dict[str, float] | None = None
    # Snapshot filter applied to the diagnostic, if any.
    snapshot_id: str | None = None


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _run_scheme(
    *,
    label_scheme: str,
    label_by_id: dict[int, float],
    vectors,
    X: np.ndarray,
    alphas: Sequence[float],
    production_alpha: float,
    pos_ids: set[int],
    neg_ids: set[int],
    weight_by_id: dict[int, float] | None = None,
) -> SchemeDiagnostic:
    """``weight_by_id`` (optional) plumbs A4's per-corridor sample
    weights through to the ridge fit. Only the weighted v2 scheme
    passes a non-None map; other schemes stay uniform-weighted."""
    summary = label_distribution_summary(label_scheme, label_by_id)
    if not label_by_id:
        return SchemeDiagnostic(
            label_scheme=label_scheme, label_summary=None,
            sweep=[], ablation_baseline=None, ablation=[], n_labeled=0,
        )
    keep_mask = np.array(
        [v.corridor_id in label_by_id for v in vectors], dtype=bool,
    )
    kept_vectors = [v for v, k in zip(vectors, keep_mask) if k]
    X_kept = X[keep_mask]
    y = np.array(
        [label_by_id[v.corridor_id] for v in kept_vectors], dtype=np.float64,
    )
    if weight_by_id is None:
        sample_weights = None
    else:
        # Missing-weight corridors fall back to 1.0 (treated as
        # unweighted); log-only edge case since the caller should
        # align label_by_id and weight_by_id keys.
        sample_weights = np.array(
            [weight_by_id.get(v.corridor_id, 1.0) for v in kept_vectors],
            dtype=np.float64,
        )

    sweep = regularization_sweep(
        vectors=kept_vectors, X=X_kept, y=y, alphas=alphas,
        feature_names=FEATURE_NAMES,
        pos_ids=pos_ids, neg_ids=neg_ids,
        sample_weights=sample_weights,
    )
    baseline, ablation = feature_ablation(
        vectors=kept_vectors, X=X_kept, y=y, alpha=production_alpha,
        feature_names=FEATURE_NAMES,
        pos_ids=pos_ids, neg_ids=neg_ids,
        sample_weights=sample_weights,
    )
    return SchemeDiagnostic(
        label_scheme=label_scheme,
        label_summary=summary,
        sweep=sweep,
        ablation_baseline=baseline,
        ablation=ablation,
        n_labeled=len(kept_vectors),
    )


def run_diagnostics(
    conn: Connection,
    *,
    alphas: Sequence[float] = DEFAULT_ALPHAS,
    production_alpha: float = 1.0,
    classification_version: str = CLASSIFICATION_VERSION,
    v2_aggregation_method: str = "trimmed_mean",
    v2_trimmed_q: float = 0.1,
    v2_outlier_sigma: float | None = 3.0,
    snapshot_id: str | None = None,
) -> DiagnosticReport:
    """End-to-end: materialize features + labels, run the three
    diagnostics per label scheme (inverse_rank, time_envelope,
    time_envelope_v2), return a combined report.

    v2 parameters control the label refinement pass (see
    docs/learning/time-envelope-label-v2.md). Defaults match the
    A2 design note.

    ``snapshot_id`` (optional) restricts the corridor + label inputs
    to a single ingestion snapshot, so the diagnostic compares
    snapshot cohorts cleanly."""
    import statistics as _stats
    rows = load_corridor_rows(
        conn,
        classification_version=classification_version,
        snapshot_id=snapshot_id,
    )
    if not rows:
        raise RuntimeError(
            "no corridor rows — "
            + (f"no corridors for snapshot={snapshot_id!r}. "
               if snapshot_id else "")
            + "run build-route-corridors first"
        )
    vectors, X = build_feature_matrix(rows)

    pos_ids = _fetch_cohort_map_ids(conn, ("tech-strong-proxy",))
    neg_ids = _fetch_cohort_map_ids(conn, ("tech-mediocre-proxy",))

    # v1 time-envelope inputs.
    mean_intervals = _load_map_mean_interval_ms(conn, snapshot_id=snapshot_id)
    # v2 time-envelope inputs — refined aggregation + variance/quality.
    v2_stats = load_map_interval_stats(
        conn,
        method=v2_aggregation_method,    # type: ignore[arg-type]
        trimmed_q=v2_trimmed_q,
        outlier_sigma=v2_outlier_sigma,
        snapshot_id=snapshot_id,
    )
    v2_labels, v2_quality = synthesize_time_envelope_v2_labels(rows, v2_stats)

    schemes: list[SchemeDiagnostic] = []
    schemes.append(_run_scheme(
        label_scheme="inverse_rank",
        label_by_id=synthesize_inverse_rank_labels(rows),
        vectors=vectors, X=X,
        alphas=alphas, production_alpha=production_alpha,
        pos_ids=pos_ids, neg_ids=neg_ids,
    ))
    schemes.append(_run_scheme(
        label_scheme="time_envelope",
        label_by_id=synthesize_time_envelope_labels(rows, mean_intervals),
        vectors=vectors, X=X,
        alphas=alphas, production_alpha=production_alpha,
        pos_ids=pos_ids, neg_ids=neg_ids,
    ))
    schemes.append(_run_scheme(
        label_scheme="time_envelope_v2",
        label_by_id=v2_labels,
        vectors=vectors, X=X,
        alphas=alphas, production_alpha=production_alpha,
        pos_ids=pos_ids, neg_ids=neg_ids,
    ))
    # A4 — same v2 labels, plus per-corridor sample weights driven by
    # observed-time CV (from A2's label_quality_weight). See
    # docs/learning/label-quality-weighted-training.md.
    if v2_labels and v2_quality:
        schemes.append(_run_scheme(
            label_scheme="time_envelope_v2_weighted",
            label_by_id=v2_labels,
            vectors=vectors, X=X,
            alphas=alphas, production_alpha=production_alpha,
            pos_ids=pos_ids, neg_ids=neg_ids,
            weight_by_id=v2_quality,
        ))

    # Summary of the v2 label_quality_weight distribution so the
    # reader sees whether the CV-based weighting has any range.
    if v2_quality:
        q_values = list(v2_quality.values())
        quality_summary = {
            "min": float(min(q_values)),
            "median": float(_stats.median(q_values)),
            "mean": float(_stats.mean(q_values)),
            "max": float(max(q_values)),
            "stdev": float(_stats.stdev(q_values)) if len(q_values) >= 2 else 0.0,
        }
    else:
        quality_summary = None

    return DiagnosticReport(
        started_at=_utcnow(),
        total_corridors=len(rows),
        maps_with_mean_interval=len(mean_intervals),
        alphas=tuple(float(a) for a in alphas),
        production_alpha=production_alpha,
        schemes=schemes,
        v2_aggregation_method=v2_aggregation_method,
        v2_map_count=len(v2_stats),
        v2_label_quality_summary=quality_summary,
        snapshot_id=snapshot_id,
    )


# ---------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------

def _fmt_float(v: float | None, places: int = 4) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.{places}f}" if v < 0 else f"{v:.{places}f}"


def _write_header(buf: io.StringIO, report: DiagnosticReport) -> None:
    buf.write("# Corridor Ranking — Score Spread Diagnostic\n\n")
    buf.write(f"- **Generated at**: `{report.started_at.isoformat()}`\n")
    if report.snapshot_id is not None:
        buf.write(f"- **Snapshot filter**: `{report.snapshot_id}`\n")
    buf.write(f"- **Total corridors**: {report.total_corridors}\n")
    buf.write(
        f"- **Maps with mean inter-CP time**: {report.maps_with_mean_interval}\n"
    )
    buf.write(f"- **Production α**: {report.production_alpha}\n")
    buf.write(
        f"- **Alpha sweep**: {', '.join(str(a) for a in report.alphas)}\n"
    )
    if report.v2_aggregation_method is not None:
        buf.write(
            f"- **v2 aggregation**: `{report.v2_aggregation_method}` "
            f"({report.v2_map_count} maps)\n"
        )
        if report.v2_label_quality_summary is not None:
            q = report.v2_label_quality_summary
            buf.write(
                f"- **v2 label_quality_weight**: "
                f"median={q['median']:.3f} mean={q['mean']:.3f} "
                f"range=[{q['min']:.3f}, {q['max']:.3f}] "
                f"stdev={q['stdev']:.3f}\n"
            )
    buf.write("\n")
    buf.write(
        "Purpose: distinguish between LABEL compression, REGULARIZATION "
        "compression, and FEATURE compression as causes of the learned "
        "corridor-score stdev being narrower than the heuristic "
        "`corridor_confidence` distribution.\n\n"
        "Signatures to look for:\n\n"
        "- **Label-bound**: label_stdev already narrow (≪ 0.173 "
        "heuristic). Model can't produce what the label doesn't have.\n"
        "- **Regularization-bound**: pred_stdev_all rises "
        "monotonically as α→0 and passes the heuristic stdev at some "
        "α. Reducing α unlocks the spread.\n"
        "- **Feature-bound**: pred_stdev_all stays flat across α AND "
        "ablation shows no single feature carries material stdev "
        "(all deltas close to 0). Features lack the range to "
        "distinguish corridors.\n\n"
        "These aren't mutually exclusive — the renderer just surfaces "
        "the numbers; interpretation is the reader's job.\n\n"
    )


def _write_label_spread(buf: io.StringIO, report: DiagnosticReport) -> None:
    buf.write("## Label distribution\n\n")
    buf.write(
        "| scheme | N | min | Q1 | median | Q3 | max | mean | stdev |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )
    for scheme in report.schemes:
        s = scheme.label_summary
        if s is None:
            buf.write(
                f"| `{scheme.label_scheme}` | 0 | _(no labels)_ | | | | | | |\n"
            )
            continue
        buf.write(
            f"| `{s.label_scheme}` | {s.count} | "
            f"{_fmt_float(s.minimum)} | {_fmt_float(s.q1)} | "
            f"{_fmt_float(s.median)} | {_fmt_float(s.q3)} | "
            f"{_fmt_float(s.maximum)} | {_fmt_float(s.mean)} | "
            f"{s.stdev:.4f} |\n"
        )
    buf.write("\n")


def _write_sweep(buf: io.StringIO, report: DiagnosticReport) -> None:
    buf.write("## Regularization sweep\n\n")
    buf.write(
        "Same feature matrix + labels, different α. `pred_stdev_all` "
        "is the stdev of `model.predict(X)` across the full scored "
        "set — the quantity the dry-run compares against the "
        "heuristic's 0.173.\n\n"
    )
    for scheme in report.schemes:
        if not scheme.sweep:
            continue
        buf.write(f"### `{scheme.label_scheme}` (N={scheme.n_labeled})\n\n")
        buf.write(
            "| α | train_rmse | test_rmse | test_rank_corr | "
            "pred_stdev_all | pred_range_all | weight_l2 | AUC (n) |\n"
            "|---|---|---|---|---|---|---|---|\n"
        )
        for r in scheme.sweep:
            auc_cell = (
                f"{r.auc_learned:.4f} ({r.n_auc_maps})"
                if r.auc_learned is not None else f"n/a ({r.n_auc_maps})"
            )
            buf.write(
                f"| {r.alpha:g} | {r.train_rmse:.4f} | {r.test_rmse:.4f} | "
                f"{_fmt_float(r.test_rank_corr)} | "
                f"{r.pred_stdev_all:.4f} | {r.pred_range_all:.4f} | "
                f"{r.weight_l2_norm:.4f} | {auc_cell} |\n"
            )
        buf.write("\n")


def _write_ablation(buf: io.StringIO, report: DiagnosticReport) -> None:
    buf.write(
        f"## Feature ablation (α = {report.production_alpha})\n\n"
    )
    buf.write(
        "Each row zeroes one feature column during training and "
        "reports the delta vs the full-feature baseline at the same "
        "α. `pred_stdev_delta < 0` = this feature carries variance "
        "the full model relies on.\n\n"
    )
    for scheme in report.schemes:
        if scheme.ablation_baseline is None:
            continue
        base = scheme.ablation_baseline
        buf.write(f"### `{scheme.label_scheme}` (N={scheme.n_labeled})\n\n")
        buf.write(
            f"Baseline: pred_stdev_all={base.pred_stdev_all:.4f}, "
            f"test_rank_corr={_fmt_float(base.test_rank_corr)}, "
            f"AUC={(f'{base.auc_learned:.4f}' if base.auc_learned is not None else 'n/a')}\n\n"
        )
        buf.write(
            "| feature | pred_stdev | Δ stdev | rank_corr | Δ rank | "
            "AUC | Δ AUC |\n"
            "|---|---|---|---|---|---|---|\n"
        )
        sorted_rows = sorted(scheme.ablation, key=lambda r: r.pred_stdev_delta)
        for r in sorted_rows:
            buf.write(
                f"| `{r.feature_name}` | {r.pred_stdev_all:.4f} | "
                f"{_fmt_float(r.pred_stdev_delta)} | "
                f"{_fmt_float(r.test_rank_corr)} | "
                f"{_fmt_float(r.test_rank_corr_delta)} | "
                f"{_fmt_float(r.auc_learned) if r.auc_learned is not None else 'n/a'} | "
                f"{_fmt_float(r.auc_delta) if r.auc_delta is not None else 'n/a'} |\n"
            )
        buf.write("\n")


def render_markdown(report: DiagnosticReport) -> str:
    buf = io.StringIO()
    _write_header(buf, report)
    _write_label_spread(buf, report)
    _write_sweep(buf, report)
    _write_ablation(buf, report)
    return buf.getvalue()


def write_report(report: DiagnosticReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(report), encoding="utf-8")
