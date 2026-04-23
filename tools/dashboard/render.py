"""Rich-markup renderers for :class:`DashboardState`.

Separated from :mod:`state` so the DB-facing code has no Textual/Rich
dependency, and the UI code has no DB dependency. Each render function
returns a string the Static widget can ``.update(...)`` with.
"""
from __future__ import annotations

from datetime import datetime, timezone

from tools.dashboard.state import (
    Bottleneck,
    Coverage,
    DashboardState,
    DiversityState,
    Health,
    LearningState,
    NextAction,
    ReadinessState,
    StageFreshness,
)


_STATUS_COLORS: dict[str, str] = {
    "GREEN": "green",
    "YELLOW": "yellow",
    "RED": "red",
    "UNKNOWN": "bright_black",
}


def _tag(status: str) -> str:
    color = _STATUS_COLORS.get(status, "bright_black")
    return f"[{color}]{status:<7}[/{color}]"


def render_health(healths: list[Health]) -> str:
    lines: list[str] = ["[b]System health[/b]"]
    if not healths:
        lines.append("  [dim]no subsystems reporting[/dim]")
        return "\n".join(lines)
    for h in healths:
        lines.append(f"  {_tag(h.status)} {h.name:<14} {h.detail}")
    return "\n".join(lines)


def _fraction_line(label: str, num: int, denom: int, width: int = 18) -> str:
    pct = (num / denom) if denom > 0 else 0.0
    return f"  {label:<{width}} [b]{num:>5}[/b] / {denom} ({pct:.0%})"


def render_coverage(c: Coverage | None) -> str:
    lines: list[str] = ["[b]Data coverage[/b]"]
    if c is None:
        lines.append("  [dim]not collected[/dim]")
        return "\n".join(lines)
    lines.append(_fraction_line("parsed",        c.maps_parsed, c.maps_total))
    lines.append(_fraction_line("with replays",  c.maps_with_replays, c.maps_total))
    lines.append(_fraction_line("with clean",    c.maps_with_clean_replays, c.maps_total))
    lines.append(_fraction_line("with corridors", c.maps_with_corridors, c.maps_total))
    # The labels pool is the key Phase-4 lever, so bold-label it.
    lines.append("")
    lines.append("  [b]label pool (time-envelope):[/b]")
    lines.append(_fraction_line(
        "  corridor ∩ clean", c.corridor_maps_with_clean_replays, c.maps_with_corridors,
    ))
    lines.append(_fraction_line(
        "  label-usable", c.maps_with_time_envelope_label, c.maps_with_corridors,
    ))
    return "\n".join(lines)


def render_bottlenecks(items: list[Bottleneck]) -> str:
    lines: list[str] = ["[b]Bottlenecks[/b]"]
    if not items:
        lines.append("  [green]no blocking issues detected[/green]")
        return "\n".join(lines)
    for b in items:
        color = _STATUS_COLORS.get(b.severity, "yellow")
        lines.append(f"  [{color}]● {b.title}[/{color}]")
        lines.append(f"      [dim]{b.detail}[/dim]")
    return "\n".join(lines)


def _humanize_age(ts: datetime | None, *, now: datetime | None = None) -> str:
    if ts is None:
        return "never"
    now = now or datetime.now(tz=timezone.utc)
    delta_s = int((now - ts).total_seconds())
    if delta_s < 0:
        return "in the future"
    if delta_s < 60:
        return f"{delta_s}s ago"
    if delta_s < 3600:
        return f"{delta_s // 60}m ago"
    if delta_s < 86_400:
        return f"{delta_s // 3600}h ago"
    return f"{delta_s // 86_400}d ago"


def render_freshness(entries: list[StageFreshness], *, now: datetime | None = None) -> str:
    lines: list[str] = ["[b]Last run[/b]"]
    if not entries:
        lines.append("  [dim]no stage runs recorded[/dim]")
        return "\n".join(lines)
    for e in entries:
        age = _humanize_age(e.completed_at, now=now)
        status_part = ""
        if e.status and e.status not in ("success", None):
            color = "red" if e.status == "failed" else "yellow"
            status_part = f" [{color}]({e.status})[/{color}]"
        lines.append(f"  {e.stage:<18} {age}{status_part}")
    return "\n".join(lines)


def render_counters(c: dict[str, int]) -> str:
    """The original counters panel — kept for the "what exists" read
    because the health+coverage panels are interpretations, not raw
    numbers, and operators sometimes want the raw numbers too."""
    if not c:
        return "[dim]no counters collected[/dim]"
    lines = ["[b]Counters[/b]"]
    lines.append(f"  placements:    [b]{c.get('replays_total', 0):>5}[/b] replays")
    lines.append(f"  breadcrumbs:   {c.get('replays_with_breadcrumbs', 0):>5}")
    lines.append(f"  corridors:     {c.get('corridors_total', 0):>5} "
                 f"({c.get('corridors_top_rank', 0)} top-rank)")
    lines.append(f"  learned score: {c.get('corridors_with_learned_score', 0):>5}")
    return "\n".join(lines)


def render_learning(state: LearningState | None) -> str:
    lines: list[str] = ["[b]Learning state[/b]"]
    if state is None or state.scored_corridors == 0:
        lines.append("  [dim]no learned scores yet — run score-corridors-learned[/dim]")
        return "\n".join(lines)
    lines.append(
        f"  {_tag(state.status)} "
        f"{state.scheme_tag or '[dim]unknown scheme[/dim]'}"
    )
    if state.model_hash_short:
        lines.append(f"  model:         {state.model_hash_short}")
    lines.append(
        f"  scored:        [b]{state.scored_corridors}[/b] corridors"
    )
    lines.append(
        f"  pred range:    [{state.pred_min:+.4f}, {state.pred_max:+.4f}] "
        f"(median {state.pred_median:+.4f})"
    )
    if state.heuristic_stdev is not None and state.stdev_ratio is not None:
        lines.append(
            f"  pred stdev:    [b]{state.pred_stdev:.4f}[/b]  "
            f"heuristic {state.heuristic_stdev:.4f}  "
            f"ratio {state.stdev_ratio:.2f}"
        )
    else:
        lines.append(
            f"  pred stdev:    [b]{state.pred_stdev:.4f}[/b]  "
            f"[dim](no heuristic baseline)[/dim]"
        )
    if state.ai_quality_score is not None:
        trend_symbol = {
            "improving": "↑",
            "flat": "→",
            "worsening": "↓",
            "unknown": "·",
        }.get(state.ai_quality_trend, "·")
        trend_color = {
            "improving": "green",
            "worsening": "red",
            "flat": "yellow",
        }.get(state.ai_quality_trend, "bright_black")
        lines.append(
            f"  AI quality:    [b]{state.ai_quality_score:.2f}[/b] / 1.00  "
            f"[{trend_color}]{trend_symbol} {state.ai_quality_trend}[/{trend_color}]"
        )
    if state.latest_test_rank_corr is not None:
        lines.append(
            f"  latest train:  test_rank_corr={state.latest_test_rank_corr:.4f}  "
            f"auc_delta={state.latest_auc_delta if state.latest_auc_delta is not None else 0:+.4f}"
        )
    return "\n".join(lines)


def render_diversity(state: DiversityState | None) -> str:
    lines: list[str] = ["[b]Diversity watchdog[/b]"]
    if state is None:
        lines.append("  [dim]not collected[/dim]")
        return "\n".join(lines)
    lines.append(f"  {_tag(state.status)} {state.reason}")
    if (
        state.heuristic_diversity_median is None
        or state.learned_diversity_median is None
    ):
        lines.append(
            f"  [dim]{state.intervals_compared} intervals — "
            "comparison unavailable[/dim]"
        )
        return "\n".join(lines)
    lines.append(
        f"  intervals:     {state.intervals_compared}"
    )
    lines.append(
        f"  heuristic div: [b]{state.heuristic_diversity_median:.4f}[/b] (median)"
    )
    lines.append(
        f"  learned div:   [b]{state.learned_diversity_median:.4f}[/b] (median)"
    )
    delta_med = state.delta_median or 0.0
    delta_mean = state.delta_mean or 0.0
    sign_m = "+" if delta_med >= 0 else ""
    sign_mean = "+" if delta_mean >= 0 else ""
    lines.append(
        f"  delta:         median {sign_m}{delta_med:.4f}  "
        f"mean {sign_mean}{delta_mean:.4f}"
    )
    if state.variety_score is not None:
        lines.append(
            f"  variety score: [b]{state.variety_score:.2f}[/b] / 1.00"
        )
    return "\n".join(lines)


def render_readiness(state: ReadinessState | None) -> str:
    lines: list[str] = ["[b]Generation readiness[/b]"]
    if state is None:
        lines.append("  [dim]not computed[/dim]")
        return "\n".join(lines)
    badge = "[green]READY[/green]" if state.ready else "[yellow]NOT READY[/yellow]"
    lines.append(f"  {badge}  ({state.fraction:.0%} of gates)")
    for r in state.reasons:
        lines.append(f"    · {r}")
    return "\n".join(lines)


def render_next_actions(actions: list[NextAction]) -> str:
    lines: list[str] = ["[b]Next best action[/b]"]
    if not actions:
        lines.append("  [dim]nothing suggested[/dim]")
        return "\n".join(lines)
    # Top action gets emphasis; the rest are collapsed under it.
    head, rest = actions[0], actions[1:]
    color = "green" if head.priority >= 6 else "cyan"
    lines.append(f"  [{color}]▶ {head.title}[/{color}]")
    lines.append(f"      [dim]{head.reason}[/dim]")
    if head.command:
        lines.append(f"      [b]$[/b] {head.command}")
    for a in rest:
        lines.append(f"  [dim]· {a.title}[/dim]")
    return "\n".join(lines)


def render_error(err: str) -> str:
    return f"[red]collection error:[/red] {err}"


def render_all(state: DashboardState) -> dict[str, str]:
    """Return one rendered string per panel id so the Dashboard can
    update widgets by name. Keys correspond to ``#<id>`` in the
    Textual CSS."""
    if state.error:
        msg = render_error(state.error)
        return {
            "health": msg,
            "coverage": msg,
            "bottlenecks": msg,
            "freshness": msg,
            "counters": msg,
            "learning": msg,
            "diversity": msg,
            "readiness": msg,
            "next_actions": msg,
        }
    return {
        "health": render_health(state.healths),
        "coverage": render_coverage(state.coverage),
        "bottlenecks": render_bottlenecks(state.bottlenecks),
        "freshness": render_freshness(state.freshness),
        "counters": render_counters(state.counters),
        "learning": render_learning(state.learning),
        "diversity": render_diversity(state.diversity),
        "readiness": render_readiness(state.readiness),
        "next_actions": render_next_actions(state.next_actions),
    }
