"""Markdown renderer for :class:`CoverageReport`.

Honest textual format — one section per bucket, a top-N
recommendation table, and a header that says what the numbers mean
and what they don't. No plots; a reader should be able to diff two
reports in plain git.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

from src.coverage.replay_value import (
    SATURATION_PER_MAP,
    CoverageReport,
    MapCoverage,
    RecommendedBackfill,
)


def _title(m: MapCoverage | RecommendedBackfill, width: int = 40) -> str:
    t = (m.title or "").strip()
    if not t:
        t = f"(map {m.map_id})"
    t = t.replace("|", "¦")
    return t[:width]


def _header(buf: io.StringIO, report: CoverageReport) -> None:
    buf.write("# Replay Coverage Expansion Report\n\n")
    buf.write(f"- **Generated**: `{datetime.now(tz=timezone.utc).isoformat()}`\n")
    buf.write(f"- **Saturation cap**: {SATURATION_PER_MAP} replays / map (TMX)\n")
    buf.write(f"- **Maps total**: {report.total_maps}\n")
    buf.write(
        f"- **Corridor-owning**: {report.corridor_owning_maps} "
        f"(only these contribute to learning signal)\n"
    )
    buf.write("\n")
    buf.write(
        "**What this report is**: a data-driven backfill plan. The "
        "**value score** estimates how much one more replay helps "
        "learned ranking. Higher = better bang-for-buck.\n\n"
    )
    buf.write(
        "**What this report is not**: a quality ranking. Popularity, "
        "awards, and rank-derived scores are deliberately excluded.\n\n"
    )


def _bucket_table(buf: io.StringIO, title: str, maps: list[MapCoverage]) -> None:
    buf.write(f"## {title}\n\n")
    if not maps:
        buf.write("_(empty — nothing to flag here)_\n\n")
        return
    buf.write(f"_{len(maps)} maps_\n\n")
    buf.write(
        "| map_id | source | title | corridors | clean | total | pct |\n"
        "|---|---|---|---|---|---|---|\n"
    )
    for m in maps:
        buf.write(
            f"| {m.map_id} | {m.source_map_id or '—'} | "
            f"{_title(m)} | {m.corridor_count} | {m.clean_replays} | "
            f"{m.total_replays} | {m.percentile_rank_clean:.0f} |\n"
        )
    buf.write("\n")


def _backfill_table(buf: io.StringIO, rows: list[RecommendedBackfill]) -> None:
    buf.write("## Top-N backfill recommendation\n\n")
    if not rows:
        buf.write("_(no candidates — all corridor-owning maps saturated?)_\n\n")
        return
    buf.write(f"_Top {len(rows)} maps by expected value-to-learning._\n\n")
    buf.write(
        "| # | map_id | source | title | corridors | clean | value | reason |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )
    for i, r in enumerate(rows, 1):
        buf.write(
            f"| {i} | {r.map_id} | {r.source_map_id or '—'} | "
            f"{_title(r)} | {r.corridor_count} | {r.clean_replays} | "
            f"{r.value_score:.3f} | {r.reason} |\n"
        )
    buf.write("\n")


def _footer(buf: io.StringIO, report: CoverageReport) -> None:
    buf.write("## Notes\n\n")
    buf.write(
        "- Value formula is v0. See "
        "`docs/learning/replay-coverage-expansion-plan.md` for the "
        "design + non-goals.\n"
    )
    buf.write(
        "- Cohort-boundary flagging uses config `cohorts.*_pct` "
        "percentiles with ±2 percentile-point tolerance.\n"
    )
    buf.write(
        f"- Saturation is hard-capped at {SATURATION_PER_MAP}/map — "
        "update `SATURATION_PER_MAP` in `src/coverage/replay_value.py` "
        "if TMX exposes a higher cap.\n"
    )


def render_markdown(report: CoverageReport) -> str:
    buf = io.StringIO()
    _header(buf, report)
    _bucket_table(buf, "Saturated maps (no remaining headroom)", report.saturated_maps)
    _bucket_table(
        buf, "Zero clean replays on corridor maps (highest marginal value)",
        report.zero_replay_corridor_maps,
    )
    _bucket_table(
        buf, "Cohort-threshold adjacent (one replay could flip bucket)",
        report.near_cohort_boundary_maps,
    )
    _backfill_table(buf, report.backfill_recommendation)
    _footer(buf, report)
    return buf.getvalue()
