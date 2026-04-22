"""Corpus-audit CLI commands.

Lives under ``src/cli/`` but imported by ``src/cli/__main__.py``
rather than registered through the global argparse tree inline. Keeps
the main CLI surface readable and lets audit queries grow without
crowding the principal pipeline stages.

Audits are **reports about the current state of the corpus**, not
normative classifications. The numbers they emit are corpus-dependent
and drift as ingestion grows — consumers should treat audit output as
"at-this-moment snapshot," not "permanent truth." Classification
decisions that drive pipeline behavior live in code (e.g., the
traversability family classification in Phase 2 of the corridor
workstream), not in audit output.

Exposed commands:
    audit-block-families   — corpus-shape audit over block_placements
                             + Neo4j adjacency + map_checkpoints.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.storage.mariadb import cursor, open_connection
from src.utils.config import code_version, load_config

_LOG = logging.getLogger(__name__)

# Checkpoint-anchor family lookup leans on the same heuristic the
# parse pipeline uses to normalize block_type → block_family, so audit
# output stays consistent with what adjacency extraction would see.
from src.parsers.pipeline import extract_block_family  # noqa: E402


def _fetch_corpus_stats(conn: Any) -> dict[str, int]:
    """Totals only — cheap and easy to reason about."""
    out: dict[str, int] = {}
    with cursor(conn) as cur:
        cur.execute("SELECT COUNT(*) FROM maps WHERE parse_status='success'")
        out["maps_parsed"] = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(DISTINCT block_family) FROM block_placements")
        out["distinct_families"] = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(DISTINCT block_type) FROM block_placements")
        out["distinct_types"] = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM block_placements WHERE is_free=0")
        out["grid_placements"] = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM block_placements WHERE is_free=1")
        out["free_placements"] = int(cur.fetchone()[0])
    return out


def _fetch_families(conn: Any, limit: int) -> list[dict[str, Any]]:
    sql = (
        "SELECT block_family, COUNT(*) AS placements, "
        "COUNT(DISTINCT map_id) AS maps_present "
        "FROM block_placements GROUP BY block_family "
        "ORDER BY placements DESC LIMIT %s"
    )
    with cursor(conn) as cur:
        cur.execute(sql, (int(limit),))
        return [
            {
                "family": str(r[0]),
                "placements": int(r[1]),
                "maps_present": int(r[2]),
            }
            for r in cur.fetchall()
        ]


def _fetch_per_map_family_stats(conn: Any, limit: int) -> list[dict[str, Any]]:
    """Per-family placement-count stats (the 'how prevalent per map' view).

    Useful for spotting families that are overall-small but per-map-
    huge — e.g. Void has only 4 maps but those maps have 20k+ void
    blocks each, which is structurally different from a family like
    Road that's spread across 776 maps with ~40 blocks each.
    """
    sql = (
        "SELECT block_family, "
        "  ROUND(AVG(per_map_n)) AS mean_per_map, "
        "  MIN(per_map_n) AS min_per_map, "
        "  MAX(per_map_n) AS max_per_map "
        "FROM (SELECT map_id, block_family, COUNT(*) AS per_map_n "
        "      FROM block_placements GROUP BY map_id, block_family) t "
        "GROUP BY block_family ORDER BY mean_per_map DESC LIMIT %s"
    )
    with cursor(conn) as cur:
        cur.execute(sql, (int(limit),))
        return [
            {
                "family": str(r[0]),
                "mean_per_map": int(r[1]) if r[1] is not None else 0,
                "min_per_map": int(r[2]) if r[2] is not None else 0,
                "max_per_map": int(r[3]) if r[3] is not None else 0,
            }
            for r in cur.fetchall()
        ]


def _fetch_adjacency_pairs(config: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Top family-pair adjacency counts from Neo4j. Returns an empty list
    (with a note in the caller) if Neo4j is unreachable — audit output
    still renders, just without the adjacency section.
    """
    try:
        from src.storage.neo4j_adapter import open_driver
    except Exception:  # noqa: BLE001
        return []
    try:
        driver = open_driver(config)
    except Exception:  # noqa: BLE001
        return []
    pairs: list[dict[str, Any]] = []
    try:
        with driver.session() as s:
            q = (
                "MATCH (a:Block)-[r:ADJACENT_TO]->(b:Block) "
                "RETURN a.family AS fa, b.family AS fb, count(r) AS n "
                "ORDER BY n DESC LIMIT $limit"
            )
            for rec in s.run(q, limit=int(limit)):
                pairs.append({
                    "src_family": rec["fa"],
                    "dst_family": rec["fb"],
                    "count": int(rec["n"]),
                })
    finally:
        driver.close()
    return pairs


def _fetch_total_adjacency(config: dict[str, Any]) -> int | None:
    try:
        from src.storage.neo4j_adapter import open_driver
    except Exception:  # noqa: BLE001
        return None
    try:
        driver = open_driver(config)
    except Exception:  # noqa: BLE001
        return None
    try:
        with driver.session() as s:
            r = s.run("MATCH ()-[r:ADJACENT_TO]->() RETURN count(r) AS n").single()
            return int(r["n"]) if r else None
    finally:
        driver.close()


def _fetch_checkpoint_anchor_families(conn: Any) -> list[dict[str, Any]]:
    """Roll up map_checkpoints.block_name into families using the same
    heuristic as block_placements. This is the "which families carry
    checkpoint/start/finish waypoints" cut that informs seed rules.
    """
    from collections import Counter
    with cursor(conn) as cur:
        cur.execute("SELECT block_name, COUNT(*) FROM map_checkpoints GROUP BY block_name")
        rows = cur.fetchall()
    counter: Counter[str] = Counter()
    for name, n in rows:
        fam = extract_block_family(str(name or ""))
        counter[fam] += int(n)
    return [
        {"family": fam, "waypoint_rows": n}
        for fam, n in counter.most_common()
    ]


def _render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Block-family audit")
    lines.append("")
    lines.append(f"- Generated at: `{report['generated_at']}`")
    lines.append(f"- Code version: `{report['code_version']}`")
    lines.append("")
    lines.append("This is a point-in-time snapshot. Numbers drift as the corpus grows.")
    lines.append("Do NOT cite these counts as permanent truth — the CLI that produced")
    lines.append("them is reproducible: `python -m src.cli audit-block-families`.")
    lines.append("")
    s = report["corpus"]
    lines.append("## Corpus")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    lines.append(f"| maps parsed | {s['maps_parsed']:,} |")
    lines.append(f"| grid placements | {s['grid_placements']:,} |")
    lines.append(f"| free placements | {s['free_placements']:,} |")
    lines.append(f"| distinct block families | {s['distinct_families']} |")
    lines.append(f"| distinct block types | {s['distinct_types']:,} |")
    if report.get("neo4j_adjacency_total") is not None:
        lines.append(f"| Neo4j ADJACENT_TO edges | {report['neo4j_adjacency_total']:,} |")
    lines.append("")

    lines.append("## Families by placement count")
    lines.append("")
    lines.append("| family | placements | maps present |")
    lines.append("|---|---:|---:|")
    for f in report["families"]:
        lines.append(f"| `{f['family']}` | {f['placements']:,} | {f['maps_present']:,} |")
    lines.append("")

    lines.append("## Families by per-map prevalence")
    lines.append("")
    lines.append("Mean placements per map when the family is present. High mean + low")
    lines.append("map-count means a family is niche-but-heavy in the maps it appears in.")
    lines.append("")
    lines.append("| family | mean/map | min/map | max/map |")
    lines.append("|---|---:|---:|---:|")
    for f in report["family_prevalence"]:
        lines.append(
            f"| `{f['family']}` | {f['mean_per_map']:,} | "
            f"{f['min_per_map']:,} | {f['max_per_map']:,} |"
        )
    lines.append("")

    pairs = report.get("adjacency_pairs", [])
    if pairs:
        lines.append("## Adjacency graph — top family-pair edge counts (Neo4j)")
        lines.append("")
        lines.append("| src family | dst family | edges |")
        lines.append("|---|---|---:|")
        for p in pairs:
            lines.append(
                f"| `{p['src_family']}` | `{p['dst_family']}` | {p['count']:,} |"
            )
        lines.append("")
    else:
        lines.append("## Adjacency graph")
        lines.append("")
        lines.append("_Neo4j unreachable at audit time; adjacency section skipped._")
        lines.append("")

    lines.append("## Checkpoint-anchor family distribution")
    lines.append("")
    lines.append("Rolled up from `map_checkpoints.block_name` via the same family")
    lines.append("heuristic used by the parse pipeline. Indicates which families")
    lines.append("carry waypoint (start / checkpoint / finish / start-finish) variants")
    lines.append("in the current corpus.")
    lines.append("")
    lines.append("| family | waypoint rows |")
    lines.append("|---|---:|")
    for f in report["checkpoint_anchor_families"]:
        lines.append(f"| `{f['family']}` | {f['waypoint_rows']:,} |")
    lines.append("")

    return "\n".join(lines) + "\n"


def _cmd_audit_block_families(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    conn = open_connection(config)
    try:
        report = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "code_version": code_version(),
            "corpus": _fetch_corpus_stats(conn),
            "families": _fetch_families(conn, args.family_limit),
            "family_prevalence": _fetch_per_map_family_stats(conn, args.family_limit),
            "adjacency_pairs": _fetch_adjacency_pairs(config, args.adjacency_limit),
            "neo4j_adjacency_total": _fetch_total_adjacency(config),
            "checkpoint_anchor_families": _fetch_checkpoint_anchor_families(conn),
        }
    finally:
        conn.close()

    # JSON output (if requested) is the machine-readable payload; markdown
    # is the human-readable side. Both get the same underlying data.
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        _LOG.info("wrote JSON report: %s", args.json)

    md = _render_markdown(report)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(md, encoding="utf-8")
        _LOG.info("wrote markdown report: %s", args.output)
    else:
        sys.stdout.write(md)
    return 0


def register_audit_commands(sub: argparse._SubParsersAction) -> None:
    """Attach audit subcommands to the main CLI parser."""
    cmd = sub.add_parser(
        "audit-block-families",
        help="Point-in-time corpus audit: families, prevalence, adjacency, anchors",
    )
    cmd.add_argument(
        "--family-limit", type=int, default=30,
        help="max number of families to list in the per-family sections (default 30)",
    )
    cmd.add_argument(
        "--adjacency-limit", type=int, default=20,
        help="max family-pair rows in the adjacency section (default 20)",
    )
    cmd.add_argument(
        "--output", type=str, default=None,
        help="write markdown report to this path (default: stdout)",
    )
    cmd.add_argument(
        "--json", type=str, default=None,
        help="also write a machine-readable JSON dump to this path",
    )
    cmd.set_defaults(func=_cmd_audit_block_families)
