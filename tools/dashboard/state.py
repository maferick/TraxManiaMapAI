"""Dashboard decision-layer state.

Status panel → decision dashboard. Instead of raw counts, surface:

- **Health** per subsystem (GREEN / YELLOW / RED)
- **Data coverage** fractions (maps with replays / corridors / usable labels)
- **Bottlenecks** — zero-valued counters that block downstream work
- **Freshness** — last completed_at per pipeline stage

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
class DashboardState:
    """One snapshot. :func:`fetch_state` builds it; render helpers
    consume it."""
    collected_at: datetime
    healths: list[Health] = field(default_factory=list)
    coverage: Coverage | None = None
    bottlenecks: list[Bottleneck] = field(default_factory=list)
    freshness: list[StageFreshness] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
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
    except Exception as exc:  # noqa: BLE001
        return DashboardState(
            collected_at=_utcnow(),
            error=f"collection failed: {exc}",
        )
    return DashboardState(
        collected_at=_utcnow(),
        healths=_compute_healths(counters),
        coverage=_compute_coverage(counters),
        bottlenecks=_compute_bottlenecks(counters),
        freshness=freshness,
        counters=counters,
    )
