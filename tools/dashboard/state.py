"""Dashboard decision-layer state.

Status panel → decision dashboard. Surfaces:

- **Health** per subsystem (GREEN / YELLOW / RED)
- **Data coverage** fractions (maps with replays / corridors / usable labels)
- **Bottlenecks** — zero-valued counters that block downstream work
- **Freshness** — last completed_at per pipeline stage
- **Learning state** — model hash, scheme tag, pred distribution stats
  (A5)
- **Diversity watchdog** — heuristic vs learned Jaccard-based
  diversity comparison (A5; per PR #25 watchdog thresholds)
- **Next best action** — rule-engine suggestions prioritised by the
  state of the other panels (A5)

Everything here is one DB snapshot → one :class:`DashboardState`.
Pure functions; no Textual dependency so the module can be tested
without launching the TUI.

Thresholds are tuned for Phase 1 scale-1k corpus. They're conservative
on purpose — a GREEN here means "no operator attention needed," not
"production-grade." Bump thresholds as the project matures.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pymysql.connections import Connection

from src.storage.mariadb import cursor


# ---------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class Health:
    """One subsystem's health. ``status`` is the traffic-light;
    ``detail`` is a one-line summary the panel displays."""
    name: str
    status: str                        # "GREEN" | "YELLOW" | "RED" | "UNKNOWN"
    detail: str


@dataclass(frozen=True)
class Coverage:
    """Data-coverage fractions. Each pair is (numerator, denominator,
    fraction). Rendered as ``23 / 100 (23%)`` — the denominator is
    what makes the number interpretable."""
    maps_total: int
    maps_parsed: int
    maps_with_replays: int
    maps_with_clean_replays: int
    maps_with_corridors: int
    corridor_maps_with_clean_replays: int
    maps_with_time_envelope_label: int


@dataclass(frozen=True)
class Bottleneck:
    """One flagged issue. ``severity`` is RED (blocks downstream work)
    or YELLOW (degraded but not blocked)."""
    severity: str                      # "RED" | "YELLOW"
    title: str
    detail: str


@dataclass(frozen=True)
class StageFreshness:
    stage: str
    completed_at: datetime | None
    status: str | None                 # last run's status (running|success|partial|failed)


@dataclass(frozen=True)
class LearningState:
    """Current learned-model snapshot from ``route_corridors``.

    Pulled from the persisted score column rather than a separate
    metrics table: the data the dashboard cares about is *what
    model is currently deployed*, not *what we trained last hour*.
    A full re-train produces a new hash + scheme tag that this
    picks up on the next refresh.

    Phase-2 PR B adds the synthetic ``ai_quality_score`` + trend for
    the formalized decision panel. Those come from
    :func:`src.learning.scores.ai_quality_score` computed live from
    the DB-resident signals plus (when available) historical training
    metrics from ``model_metrics``.
    """
    scheme_tag: str | None               # e.g. "time_envelope_v2_weighted@0.1.0"
    model_hash_short: str | None         # first 12 hex chars of sha256
    scored_corridors: int
    pred_min: float
    pred_median: float
    pred_max: float
    pred_mean: float
    pred_stdev: float
    heuristic_stdev: float | None        # for the stdev-ratio comparison
    stdev_ratio: float | None            # pred_stdev / heuristic_stdev
    status: str                          # GREEN / YELLOW / RED / UNKNOWN
    # Synthetic AI Quality score + trend (PR B). Score in [0, 1] combining
    # rank_corr / stdev_ratio / AUC delta axes; trend is one of
    # "improving" / "flat" / "worsening" / "unknown".
    ai_quality_score: float | None = None
    ai_quality_trend: str = "unknown"
    # Latest training's test-set rank correlation + AUC delta, sourced
    # from model_metrics (NULL when no training has been recorded).
    latest_test_rank_corr: float | None = None
    latest_auc_delta: float | None = None


@dataclass(frozen=True)
class DiversityState:
    """A3 watchdog snapshot. Compares heuristic vs learned top-K
    diversity (Jaccard-on-cells) to catch ranker collapse.

    Thresholds match the PR #25 baseline — diversity delta of
    -0.10 median or worse is RED; -0.05 to -0.10 is YELLOW;
    -0.05 or better (incl. positive) is GREEN.

    Phase-2 PR B adds ``variety_score`` in [0, 1] derived from
    ``delta_median`` for the operator-facing panel.
    """
    intervals_compared: int
    heuristic_diversity_median: float | None
    learned_diversity_median: float | None
    delta_median: float | None
    delta_mean: float | None
    status: str                          # GREEN / YELLOW / RED / UNKNOWN
    reason: str
    variety_score: float | None = None


@dataclass(frozen=True)
class ReadinessState:
    """Phase-2 generation-readiness summary. A green ``ready`` flag
    means all gates passed; ``reasons`` explains each gate in order.
    ``fraction`` in [0, 1] surfaces partial progress for the UI."""
    ready: bool
    fraction: float
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NextAction:
    """One suggested action, ordered by ``priority`` (lower = higher
    priority). ``command`` is a copy-pasteable CLI invocation where
    relevant; empty string for analysis-only suggestions."""
    priority: int                        # 1 (highest) upward
    title: str
    reason: str
    command: str = ""


@dataclass(frozen=True)
class DashboardState:
    """One snapshot. :func:`fetch_state` builds it; render helpers
    consume it."""
    collected_at: datetime
    healths: list[Health] = field(default_factory=list)
    coverage: Coverage | None = None
    bottlenecks: list[Bottleneck] = field(default_factory=list)
    freshness: list[StageFreshness] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    learning: LearningState | None = None
    diversity: DiversityState | None = None
    readiness: ReadinessState | None = None
    next_actions: list[NextAction] = field(default_factory=list)
    error: str | None = None           # set when collection failed; UI shows this verbatim


# ---------------------------------------------------------------------
# Thresholds — named so they're easy to review in one place
# ---------------------------------------------------------------------

# Ingest: success here is high because failures above 10% means TMX
# pagination or parser wrapper is broken.
_INGEST_GREEN = 0.95
_INGEST_YELLOW = 0.80

# Replay clean: 50% of processed replays should survive cleaning on
# a real corpus; below 10% implies calibration drift.
_REPLAY_CLEAN_GREEN = 0.50
_REPLAY_CLEAN_YELLOW = 0.10

# Corridor coverage: below-floor means the enumerator didn't run or
# classified nothing as drivable.
_CORRIDOR_MAPS_GREEN = 100
_CORRIDOR_MAPS_YELLOW = 10

# Learning: what fraction of top-rank corridors has a persisted
# learned score. Zero means "not scored yet."
_LEARNING_SCORED_GREEN = 0.80
_LEARNING_SCORED_YELLOW = 0.01

# Learning distribution: stdev ratio = learned_stdev / heuristic_stdev.
# Heuristic hand-tuned distribution is roughly 0.17 on scale-1k; the
# learned model wants to be at least comparable in expressiveness.
# Post-A4 we measured ratio ≈ 0.60 — that's the current GREEN anchor.
_LEARNING_STDEV_RATIO_GREEN = 0.50
_LEARNING_STDEV_RATIO_YELLOW = 0.20

# Diversity watchdog: PR #25 baseline was delta_median -0.068 under
# the pre-A4 ranker. Post-A4 we landed at -0.039, so the GREEN band
# needs to be tolerant of small negatives while catching regressions.
# delta = learned_diversity - heuristic_diversity; more negative ⇒
# learned collapses more.
_DIVERSITY_DELTA_GREEN = -0.05
_DIVERSITY_DELTA_YELLOW = -0.10


# ---------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------

def _scalar(cur: Any, sql: str, params: tuple = ()) -> int:
    cur.execute(sql, params)
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _fetch_counters(cur: Any) -> dict[str, int]:
    """Counters we need across multiple sections. Centralized so the
    thresholds and the pretty-printed panels share one source."""
    c: dict[str, int] = {}
    c["maps_total"] = _scalar(cur, "SELECT COUNT(*) FROM maps")
    c["maps_parsed"] = _scalar(
        cur, "SELECT COUNT(*) FROM maps WHERE parse_status='success'"
    )
    c["maps_failed_permanent"] = _scalar(
        cur, "SELECT COUNT(*) FROM maps WHERE parse_status='failed_permanent'"
    )
    c["replays_total"] = _scalar(cur, "SELECT COUNT(*) FROM replays")
    c["replays_clean"] = _scalar(
        cur,
        "SELECT COUNT(*) FROM replays WHERE clean_status IN ('clean','usable_with_warnings')",
    )
    c["replays_rejected"] = _scalar(
        cur, "SELECT COUNT(*) FROM replays WHERE clean_status='rejected'"
    )
    c["replays_processed"] = _scalar(
        cur,
        "SELECT COUNT(*) FROM replays WHERE clean_status IN ('clean','usable_with_warnings','rejected')",
    )
    c["replays_with_breadcrumbs"] = _scalar(
        cur, "SELECT COUNT(*) FROM replays WHERE breadcrumbs_path IS NOT NULL"
    )
    c["replays_with_cohort"] = _scalar(
        cur, "SELECT COUNT(*) FROM replays WHERE cohort_membership IS NOT NULL"
    )
    c["maps_with_any_replay"] = _scalar(
        cur, "SELECT COUNT(DISTINCT map_id) FROM replays"
    )
    c["maps_with_clean_replays"] = _scalar(
        cur,
        "SELECT COUNT(DISTINCT map_id) FROM replays "
        "WHERE clean_status IN ('clean','usable_with_warnings')",
    )
    c["maps_with_corridors"] = _scalar(
        cur, "SELECT COUNT(DISTINCT map_id) FROM route_corridors"
    )
    c["corridor_maps_with_clean_replays"] = _scalar(
        cur,
        "SELECT COUNT(DISTINCT r.map_id) FROM replays r "
        "JOIN route_corridors rc ON rc.map_id=r.map_id "
        "WHERE r.clean_status IN ('clean','usable_with_warnings')",
    )
    c["corridors_total"] = _scalar(cur, "SELECT COUNT(*) FROM route_corridors")
    c["corridors_top_rank"] = _scalar(
        cur, "SELECT COUNT(*) FROM route_corridors WHERE path_rank=0"
    )
    c["corridors_with_learned_score"] = _scalar(
        cur,
        "SELECT COUNT(*) FROM route_corridors "
        "WHERE path_rank=0 AND learned_corridor_score IS NOT NULL",
    )
    # Proxy for "maps with a usable time-envelope label" — needs
    # clean breadcrumb replays AND at least one corridor.
    c["maps_with_time_envelope_label"] = _scalar(
        cur,
        "SELECT COUNT(DISTINCT r.map_id) FROM replays r "
        "JOIN route_corridors rc ON rc.map_id=r.map_id "
        "WHERE r.clean_status IN ('clean','usable_with_warnings') "
        "AND r.breadcrumbs_path IS NOT NULL",
    )
    return c


def _bucket(value: float, green: float, yellow: float) -> str:
    """Upper-bounded: value >= green → GREEN; >= yellow → YELLOW; else RED."""
    if value >= green:
        return "GREEN"
    if value >= yellow:
        return "YELLOW"
    return "RED"


def _compute_healths(c: dict[str, int]) -> list[Health]:
    healths: list[Health] = []

    # Ingest: parse success rate.
    if c["maps_total"] > 0:
        rate = c["maps_parsed"] / c["maps_total"]
        status = _bucket(rate, _INGEST_GREEN, _INGEST_YELLOW)
        detail = (
            f"parse success {c['maps_parsed']} / {c['maps_total']} "
            f"({rate:.0%}); {c['maps_failed_permanent']} permanent failures"
        )
    else:
        status = "UNKNOWN"
        detail = "no maps ingested yet"
    healths.append(Health("ingest", status, detail))

    # Replay clean: how much survives cleaning.
    if c["replays_processed"] > 0:
        rate = c["replays_clean"] / c["replays_processed"]
        status = _bucket(rate, _REPLAY_CLEAN_GREEN, _REPLAY_CLEAN_YELLOW)
        detail = (
            f"clean {c['replays_clean']} / {c['replays_processed']} "
            f"({rate:.0%}); {c['replays_rejected']} rejected"
        )
    else:
        status = "UNKNOWN"
        detail = "no replays processed"
    healths.append(Health("replay_clean", status, detail))

    # Cohorts: fraction of clean replays assigned to a cohort.
    if c["replays_clean"] > 0:
        rate = c["replays_with_cohort"] / c["replays_clean"]
        if c["replays_with_cohort"] == 0:
            status = "RED"
            detail = (
                f"0 / {c['replays_clean']} clean replays have cohort "
                f"membership — run assign-cohorts"
            )
        else:
            status = _bucket(rate, 0.10, 0.01)
            detail = (
                f"{c['replays_with_cohort']} / {c['replays_clean']} "
                f"clean replays assigned ({rate:.0%})"
            )
    else:
        status = "UNKNOWN"
        detail = "no clean replays yet"
    healths.append(Health("cohorts", status, detail))

    # Corridors: how many maps have corridor enumeration.
    n = c["maps_with_corridors"]
    if n >= _CORRIDOR_MAPS_GREEN:
        status = "GREEN"
    elif n >= _CORRIDOR_MAPS_YELLOW:
        status = "YELLOW"
    else:
        status = "RED" if n == 0 else "YELLOW"
    detail = f"{n} maps have corridors ({c['corridors_total']} rows)"
    healths.append(Health("corridors", status, detail))

    # Learning: learned_corridor_score coverage on top-rank corridors.
    if c["corridors_top_rank"] > 0:
        rate = c["corridors_with_learned_score"] / c["corridors_top_rank"]
        status = _bucket(rate, _LEARNING_SCORED_GREEN, _LEARNING_SCORED_YELLOW)
        detail = (
            f"{c['corridors_with_learned_score']} / {c['corridors_top_rank']} "
            f"top-rank corridors scored ({rate:.0%})"
        )
    else:
        status = "UNKNOWN"
        detail = "no corridors to score"
    healths.append(Health("learning", status, detail))

    return healths


def _compute_coverage(c: dict[str, int]) -> Coverage:
    return Coverage(
        maps_total=c["maps_total"],
        maps_parsed=c["maps_parsed"],
        maps_with_replays=c["maps_with_any_replay"],
        maps_with_clean_replays=c["maps_with_clean_replays"],
        maps_with_corridors=c["maps_with_corridors"],
        corridor_maps_with_clean_replays=c["corridor_maps_with_clean_replays"],
        maps_with_time_envelope_label=c["maps_with_time_envelope_label"],
    )


def _compute_bottlenecks(c: dict[str, int]) -> list[Bottleneck]:
    """Turn flagged counters into actionable lines. Rule order matters
    — the list reads top-to-bottom as a punch list. RED before YELLOW."""
    out: list[Bottleneck] = []

    # Cohort assignment gap — user called this out explicitly and it's
    # a canonical Phase-1 blocker for downstream learning work.
    if c["replays_clean"] > 0 and c["replays_with_cohort"] == 0:
        out.append(Bottleneck(
            severity="RED",
            title="No cohort-labeled replays",
            detail=(
                f"{c['replays_clean']} clean replays have zero cohort "
                "membership. Run: python -m src.cli assign-cohorts"
            ),
        ))

    # Learned scoring missing on top-rank corridors.
    if c["corridors_top_rank"] > 0 and c["corridors_with_learned_score"] == 0:
        out.append(Bottleneck(
            severity="RED",
            title="No learned corridor scores",
            detail=(
                f"{c['corridors_top_rank']} top-rank corridors have no "
                "learned score. Run: score-corridors-learned "
                "--model-report reports/corridor-ranking-model-v2.json"
            ),
        ))

    # Corridor-owning maps without clean replays — shrinks the
    # time-envelope label pool.
    if c["maps_with_corridors"] > 0:
        uncovered = (
            c["maps_with_corridors"] - c["corridor_maps_with_clean_replays"]
        )
        if uncovered > 0 and uncovered / c["maps_with_corridors"] > 0.3:
            out.append(Bottleneck(
                severity="YELLOW",
                title="Replay coverage thin on corridor maps",
                detail=(
                    f"{uncovered} of {c['maps_with_corridors']} corridor "
                    f"maps lack a clean replay. Consider deeper replay "
                    "ingest (TMX caps at 25/map)."
                ),
            ))

    # Permanent parse failures — rare but worth surfacing.
    if c["maps_failed_permanent"] > 0:
        out.append(Bottleneck(
            severity="YELLOW",
            title="Maps with permanent parse failures",
            detail=(
                f"{c['maps_failed_permanent']} maps rejected by parser. "
                "Inspect parse_error_code distribution."
            ),
        ))

    return out


def _fetch_learning_state(cur: Any) -> LearningState:
    """Single-row summary of the currently-deployed learned model.

    Reads directly from ``route_corridors``: whatever scheme/hash
    scored the most rows is treated as "the deployed model." If the
    DB has multiple scheme tags (e.g. mid-migration), we surface the
    dominant one and note the count — dashboards shouldn't hide
    inconsistent state."""
    cur.execute(
        """
        SELECT
            COUNT(*) AS n,
            COALESCE(MIN(learned_corridor_score), 0),
            COALESCE(MAX(learned_corridor_score), 0),
            COALESCE(AVG(learned_corridor_score), 0),
            COALESCE(STDDEV_SAMP(learned_corridor_score), 0),
            COALESCE(STDDEV_SAMP(corridor_confidence), 0)
        FROM route_corridors
        WHERE learned_corridor_score IS NOT NULL
        """
    )
    n, p_min, p_max, p_mean, p_stdev, h_stdev = cur.fetchone()
    n = int(n or 0)
    # Median via a separate query — MariaDB lacks a scalar MEDIAN().
    if n > 0:
        cur.execute(
            """
            SELECT learned_corridor_score
            FROM route_corridors
            WHERE learned_corridor_score IS NOT NULL
            ORDER BY learned_corridor_score
            LIMIT 1 OFFSET %s
            """,
            (n // 2,),
        )
        median_row = cur.fetchone()
        p_median = float(median_row[0]) if median_row else 0.0
    else:
        p_median = 0.0
    # Dominant (scheme, hash) pair by row count.
    cur.execute(
        """
        SELECT learned_score_version, learned_score_model_hash, COUNT(*)
        FROM route_corridors
        WHERE learned_corridor_score IS NOT NULL
        GROUP BY 1, 2
        ORDER BY 3 DESC
        LIMIT 1
        """
    )
    provenance = cur.fetchone()
    if provenance is None:
        scheme_tag = None
        hash_short = None
    else:
        scheme_tag = str(provenance[0]) if provenance[0] is not None else None
        raw_hash = str(provenance[1]) if provenance[1] is not None else ""
        hash_short = raw_hash[:12] if raw_hash else None

    h_stdev_f = float(h_stdev) if h_stdev else 0.0
    p_stdev_f = float(p_stdev) if p_stdev else 0.0
    ratio: float | None
    if h_stdev_f > 0 and n > 0:
        ratio = p_stdev_f / h_stdev_f
        if ratio >= _LEARNING_STDEV_RATIO_GREEN:
            status = "GREEN"
        elif ratio >= _LEARNING_STDEV_RATIO_YELLOW:
            status = "YELLOW"
        else:
            status = "RED"
    elif n == 0:
        ratio = None
        status = "UNKNOWN"
    else:
        ratio = None
        status = "UNKNOWN"

    # PR B: augment with training-history metrics (model_metrics table).
    # Best-effort — if the table doesn't exist or a deploy predates the
    # migration, leave the extras None and log-only.
    latest_rank_corr: float | None = None
    latest_auc_delta: float | None = None
    ai_quality: float | None = None
    ai_quality_trend: str = "unknown"
    if scheme_tag is not None:
        scheme_key = scheme_tag.split("@", 1)[0]
        try:
            cur.execute(
                "SELECT test_rank_corr, auc_delta "
                "FROM model_metrics WHERE scheme = %s "
                "ORDER BY recorded_at DESC LIMIT 1",
                (scheme_key,),
            )
            latest = cur.fetchone()
            if latest is not None:
                latest_rank_corr = (
                    float(latest[0]) if latest[0] is not None else None
                )
                latest_auc_delta = (
                    float(latest[1]) if latest[1] is not None else None
                )
            cur.execute(
                "SELECT ai_quality_score FROM model_metrics "
                "WHERE scheme = %s "
                "ORDER BY recorded_at DESC LIMIT 20",
                (scheme_key,),
            )
            history_rows = cur.fetchall()
        except Exception:  # noqa: BLE001
            history_rows = []
    else:
        history_rows = []

    # Compute live ai_quality_score from the currently-observed axes.
    # Uses the training-time rank_corr + auc_delta when available, plus
    # the live DB-derived stdev_ratio. See src.learning.scores.
    from src.learning import QualityInputs, ai_quality_score as _ai_q
    from src.learning import TrendSample, trend_direction as _trend
    ai_quality = _ai_q(QualityInputs(
        test_rank_corr=latest_rank_corr,
        pred_stdev_ratio=ratio,
        auc_delta=latest_auc_delta,
    ))
    # Trend uses historical ai_quality_score entries from the table
    # (oldest → newest after reversing).
    hist_samples = [
        TrendSample(
            recorded_at_unix=0.0,  # not used for direction inference
            value=float(r[0]) if r[0] is not None else None,
        )
        for r in reversed(list(history_rows))
    ]
    if hist_samples:
        ai_quality_trend = _trend(hist_samples)

    return LearningState(
        scheme_tag=scheme_tag,
        model_hash_short=hash_short,
        scored_corridors=n,
        pred_min=float(p_min),
        pred_median=p_median,
        pred_max=float(p_max),
        pred_mean=float(p_mean),
        pred_stdev=p_stdev_f,
        heuristic_stdev=(h_stdev_f if h_stdev_f > 0 else None),
        stdev_ratio=ratio,
        status=status,
        ai_quality_score=ai_quality,
        ai_quality_trend=ai_quality_trend,
        latest_test_rank_corr=latest_rank_corr,
        latest_auc_delta=latest_auc_delta,
    )


def _compute_diversity_state(conn: Connection) -> DiversityState:
    """Compute the A3 watchdog metric on-the-fly.

    Not cached: the metric is cheap enough for scale-1k (< 1s on
    898 corridors) and caching introduces staleness + a cache-
    invalidation problem we don't need yet. If the corpus grows
    beyond ~10k corridors, add a TTL."""
    # Soft-import so the dashboard module doesn't require the
    # diversity module to exist if someone strips it out.
    try:
        from src.diversity.metrics import build_report, fetch_paths
    except Exception as exc:  # noqa: BLE001
        return DiversityState(
            intervals_compared=0,
            heuristic_diversity_median=None,
            learned_diversity_median=None,
            delta_median=None,
            delta_mean=None,
            status="UNKNOWN",
            reason=f"diversity module unavailable: {exc}",
        )
    try:
        paths = fetch_paths(conn)
    except Exception as exc:  # noqa: BLE001
        return DiversityState(
            intervals_compared=0,
            heuristic_diversity_median=None,
            learned_diversity_median=None,
            delta_median=None,
            delta_mean=None,
            status="UNKNOWN",
            reason=f"diversity fetch failed: {exc}",
        )
    report = build_report(paths, k=3)
    h = report.heuristic_summary
    l = report.learned_summary
    if h is None or l is None:
        # Missing on either side → we can't compare. Common cause:
        # score-corridors or score-corridors-learned hasn't run yet.
        reason_parts = []
        if h is None:
            reason_parts.append("no heuristic scores")
        if l is None:
            reason_parts.append("no learned scores")
        return DiversityState(
            intervals_compared=0,
            heuristic_diversity_median=(
                h.diversity_median if h is not None else None
            ),
            learned_diversity_median=(
                l.diversity_median if l is not None else None
            ),
            delta_median=None,
            delta_mean=None,
            status="UNKNOWN",
            reason="; ".join(reason_parts) or "no data",
        )
    delta_median = l.diversity_median - h.diversity_median
    delta_mean = l.diversity_mean - h.diversity_mean
    if delta_median >= _DIVERSITY_DELTA_GREEN:
        status = "GREEN"
        reason = "learned and heuristic diversity within tolerance"
    elif delta_median >= _DIVERSITY_DELTA_YELLOW:
        status = "YELLOW"
        reason = f"learned collapses {-delta_median:.3f} below heuristic (median)"
    else:
        status = "RED"
        reason = (
            f"learned collapses {-delta_median:.3f} below heuristic "
            "— investigate"
        )
    from src.learning import variety_score as _variety_score
    return DiversityState(
        intervals_compared=l.intervals_compared,
        heuristic_diversity_median=h.diversity_median,
        learned_diversity_median=l.diversity_median,
        delta_median=delta_median,
        delta_mean=delta_mean,
        status=status,
        reason=reason,
        variety_score=_variety_score(delta_median),
    )


def _compute_readiness(
    learning: LearningState | None,
    diversity: DiversityState | None,
    counters: dict[str, int],
) -> ReadinessState:
    """Phase-2 readiness summary. Thin wrapper over
    :func:`src.learning.generation_readiness` that pulls inputs from
    the dashboard's own dataclasses."""
    from src.learning import generation_readiness as _gen_ready
    label_coverage: float | None
    if counters.get("maps_with_corridors", 0) > 0:
        label_coverage = (
            counters.get("maps_with_time_envelope_label", 0)
            / counters["maps_with_corridors"]
        )
    else:
        label_coverage = None
    learned_coverage: float | None
    if counters.get("corridors_top_rank", 0) > 0:
        learned_coverage = (
            counters.get("corridors_with_learned_score", 0)
            / counters["corridors_top_rank"]
        )
    else:
        learned_coverage = None
    report = _gen_ready(
        ai_quality=(learning.ai_quality_score if learning is not None else None),
        variety=(diversity.variety_score if diversity is not None else None),
        label_coverage=label_coverage,
        learned_coverage=learned_coverage,
    )
    return ReadinessState(
        ready=report.ready,
        fraction=report.fraction,
        reasons=list(report.reasons),
    )


def _compute_next_actions(
    *,
    healths: list[Health],
    bottlenecks: list[Bottleneck],
    learning: LearningState | None,
    diversity: DiversityState | None,
    readiness: ReadinessState | None = None,
) -> list[NextAction]:
    """Rule engine. Priority order (lower wins):

    1  run assign-cohorts (if cohorts RED)
    2  run score-corridors-learned (if learning scored coverage RED)
    3  investigate diversity regression (if diversity RED)
    4  refresh learned scoring (if learning stdev ratio RED, i.e.
       the deployed model compresses severely — retrain candidate)
    5  retrain AI (if AI quality trend is worsening on a recent run)
    6  backfill replay coverage (if replay coverage thin YELLOW)
    7  ready to generate (if readiness.ready is True)
    8  healthy — consider next phase

    The list is ordered so the top entry is always the highest-
    leverage next move."""
    health_by_name = {h.name: h for h in healths}
    actions: list[NextAction] = []

    cohorts = health_by_name.get("cohorts")
    if cohorts and cohorts.status == "RED":
        actions.append(NextAction(
            priority=1,
            title="Assign cohorts to clean replays",
            reason=cohorts.detail,
            command="python -m src.cli assign-cohorts",
        ))

    learn_health = health_by_name.get("learning")
    if learn_health and learn_health.status == "RED":
        actions.append(NextAction(
            priority=2,
            title="Score corridors with the latest model",
            reason=learn_health.detail,
            command=(
                "python -m src.cli score-corridors-learned "
                "--model-report reports/corridor-ranking-model-v2-weighted.json"
            ),
        ))

    if diversity is not None and diversity.status == "RED":
        actions.append(NextAction(
            priority=3,
            title="Investigate ranker diversity collapse",
            reason=diversity.reason,
            command="python -m src.cli diagnose-corridor-diversity --output reports/corridor-diversity-watchdog.md",
        ))

    if learning is not None and learning.status == "RED":
        actions.append(NextAction(
            priority=4,
            title="Refresh learned scoring (ratio below floor)",
            reason=(
                f"pred stdev {learning.pred_stdev:.4f} vs heuristic "
                f"{learning.heuristic_stdev or 0:.4f} "
                f"(ratio {(learning.stdev_ratio or 0):.2f}); "
                "train a new model and re-score"
            ),
            command=(
                "python -m src.cli diagnose-corridor-ranking "
                "--output reports/corridor-ranking-diagnostics.md"
            ),
        ))

    if learning is not None and learning.ai_quality_trend == "worsening":
        actions.append(NextAction(
            priority=5,
            title="Retrain the model (AI Quality trending down)",
            reason=(
                "ai_quality_score history shows a worsening trend. "
                "A retrain may refresh the signal or expose a data "
                "regression worth investigating."
            ),
            command="python -m src.cli train-corridor-ranking "
                    "--output reports/corridor-ranking-model-latest.json",
        ))

    for b in bottlenecks:
        if b.severity == "YELLOW" and "coverage" in b.title.lower():
            actions.append(NextAction(
                priority=6,
                title="Consider targeted replay backfill",
                reason=b.detail,
                command=(
                    "python -m src.cli report-replay-coverage "
                    "--snapshot 2026-04-scale-1k "
                    "--output reports/replay-coverage-expansion.md"
                ),
            ))
            break   # one coverage suggestion is enough

    if (
        readiness is not None and readiness.ready
        and not actions                             # nothing else pending
    ):
        actions.append(NextAction(
            priority=7,
            title="Ready to generate",
            reason=(
                "All readiness gates passed (AI Quality, Variety, "
                "Label coverage, Learned coverage). Safe to kick off "
                "the generate action once PR C's design doc lands."
            ),
            command="",
        ))

    if not actions:
        actions.append(NextAction(
            priority=8,
            title="System is healthy",
            reason="No blocking issues detected. Consider the next "
                   "phase (OpenPlanet telemetry / generation scoping).",
        ))

    actions.sort(key=lambda a: a.priority)
    return actions


def _fetch_freshness(cur: Any) -> list[StageFreshness]:
    """Latest completed_at per stage. Uses the stage_runs table
    directly so any stage that records a run lights up here."""
    cur.execute(
        """
        SELECT s.stage, s.completed_at, s.status
        FROM stage_runs s
        INNER JOIN (
            SELECT stage, MAX(COALESCE(completed_at, started_at)) AS latest
            FROM stage_runs GROUP BY stage
        ) latest
          ON latest.stage = s.stage
         AND latest.latest = COALESCE(s.completed_at, s.started_at)
        ORDER BY s.stage
        """
    )
    rows = cur.fetchall()
    return [
        StageFreshness(
            stage=str(r[0]),
            completed_at=(
                r[1].replace(tzinfo=timezone.utc) if r[1] is not None else None
            ),
            status=str(r[2]) if r[2] is not None else None,
        )
        for r in rows
    ]


def fetch_state(conn: Connection) -> DashboardState:
    """One-shot DB snapshot. Failure is captured into ``error`` rather
    than raised — the UI renders the message verbatim."""
    try:
        with cursor(conn) as cur:
            counters = _fetch_counters(cur)
            freshness = _fetch_freshness(cur)
            learning = _fetch_learning_state(cur)
    except Exception as exc:  # noqa: BLE001
        return DashboardState(
            collected_at=_utcnow(),
            error=f"collection failed: {exc}",
        )
    # Diversity metric opens its own cursor (pulls all corridors).
    # Wrapped so a metric-side failure doesn't wipe the dashboard.
    try:
        diversity = _compute_diversity_state(conn)
    except Exception as exc:  # noqa: BLE001
        diversity = DiversityState(
            intervals_compared=0,
            heuristic_diversity_median=None,
            learned_diversity_median=None,
            delta_median=None,
            delta_mean=None,
            status="UNKNOWN",
            reason=f"diversity compute failed: {exc}",
        )
    healths = _compute_healths(counters)
    bottlenecks = _compute_bottlenecks(counters)
    readiness = _compute_readiness(learning, diversity, counters)
    next_actions = _compute_next_actions(
        healths=healths, bottlenecks=bottlenecks,
        learning=learning, diversity=diversity,
        readiness=readiness,
    )
    return DashboardState(
        collected_at=_utcnow(),
        healths=healths,
        coverage=_compute_coverage(counters),
        bottlenecks=bottlenecks,
        freshness=freshness,
        counters=counters,
        learning=learning,
        diversity=diversity,
        readiness=readiness,
        next_actions=next_actions,
    )
