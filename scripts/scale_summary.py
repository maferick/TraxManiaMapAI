"""One-shot summary of a completed scale-test run.

Invoked as:
    .venv/bin/python scripts/scale_summary.py <snapshot_id>

Prints the Milestone A kill-switch rates plus top-line distributions
(mood, scenery, adjacency validity) so the tail end of a big ingest is
easy to eyeball without handcrafting queries each time.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.mariadb import open_connection  # noqa: E402
from src.storage.neo4j_adapter import open_driver  # noqa: E402
from src.utils.config import load_config  # noqa: E402


def _pct(numer: int, denom: int) -> str:
    if denom == 0:
        return "n/a"
    return f"{(numer / denom) * 100:.1f}%"


def summarize(snapshot_id: str) -> None:
    config = load_config()
    conn = open_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM maps WHERE ingestion_snapshot=%s", (snapshot_id,))
            (total,) = cur.fetchone()
            cur.execute(
                "SELECT parse_status, COUNT(*) FROM maps WHERE ingestion_snapshot=%s "
                "GROUP BY parse_status",
                (snapshot_id,),
            )
            parse_by = dict(cur.fetchall())
            cur.execute(
                "SELECT parse_error_code, COUNT(*) FROM maps "
                "WHERE ingestion_snapshot=%s AND parse_error_code IS NOT NULL "
                "GROUP BY parse_error_code ORDER BY 2 DESC",
                (snapshot_id,),
            )
            errs = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*) FROM maps WHERE ingestion_snapshot=%s AND raw_artifact_hash IS NOT NULL",
                (snapshot_id,),
            )
            (downloaded,) = cur.fetchone()
            cur.execute(
                "SELECT mood, COUNT(*) FROM maps WHERE ingestion_snapshot=%s AND mood IS NOT NULL "
                "GROUP BY mood ORDER BY 2 DESC",
                (snapshot_id,),
            )
            moods = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*), SUM(has_custom_items), "
                "SUM(scenery_item_count), SUM(signpost_count), "
                "SUM(scenery_standard_item_count), SUM(scenery_custom_item_count) "
                "FROM maps WHERE ingestion_snapshot=%s AND decoration_parse_status='success'",
                (snapshot_id,),
            )
            n_scn, n_cust_maps, s_items, s_sign, s_std, s_cstm = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*), SUM(is_free=0), SUM(is_free=1) FROM block_placements "
                "WHERE map_id IN (SELECT id FROM maps WHERE ingestion_snapshot=%s)",
                (snapshot_id,),
            )
            bp_total, bp_grid, bp_free = cur.fetchone()
            cur.execute(
                "SELECT status, output_summary FROM stage_runs "
                "WHERE input_ref LIKE %s ORDER BY id DESC LIMIT 10",
                (f"%{snapshot_id}%",),
            )
            stage_runs = cur.fetchall()
    finally:
        conn.close()

    print(f"=== Scale test summary: {snapshot_id} ===\n")
    print("Ingest")
    print(f"  maps total:               {total}")
    print(f"  with artifact downloaded: {downloaded} ({_pct(downloaded, total)})")

    parsed = parse_by.get("success", 0)
    t_fail = parse_by.get("failed_transient", 0)
    p_fail = parse_by.get("failed_permanent", 0)
    unparsed = parse_by.get("unparsed", 0)
    print("\nParse")
    print(f"  parse_status=success:           {parsed} ({_pct(parsed, total)})")
    print(f"  parse_status=failed_transient:  {t_fail}")
    print(f"  parse_status=failed_permanent:  {p_fail}")
    print(f"  parse_status=unparsed:          {unparsed}")
    if errs:
        print("  error codes (top):")
        for code, n in errs:
            print(f"    {code}: {n}")
    print(f"\n  block_placements rows: {int(bp_total or 0)} (grid {int(bp_grid or 0)}, free {int(bp_free or 0)})")

    print("\nScenery (parsed maps)")
    print(f"  decoration_parse_status=success: {int(n_scn or 0)}")
    if n_scn:
        print(f"  maps with custom items:          {int(n_cust_maps or 0)} ({_pct(int(n_cust_maps or 0), int(n_scn))})")
        print(f"  total items={int(s_items or 0)}  signposts={int(s_sign or 0)}  "
              f"standard={int(s_std or 0)}  custom={int(s_cstm or 0)}")
        print("  mood distribution:")
        for mood, n in moods:
            print(f"    {mood}: {n} ({_pct(n, int(n_scn))})")

    # Neo4j stats
    try:
        driver = open_driver(config)
        with driver.session() as s:
            nodes = s.run("MATCH (b:Block) RETURN count(b) AS n").single()["n"]
            edges = s.run("MATCH ()-[r:ADJACENT_TO]->() RETURN count(r) AS n").single()["n"]
            labels = s.run(
                "MATCH ()-[r:ADJACENT_TO]->() "
                "WHERE r.last_seen_snapshot = $s OR r.first_seen_snapshot = $s "
                "RETURN r.validity_label AS l, count(r) AS n ORDER BY n DESC",
                s=snapshot_id,
            ).data()
            processed = s.run(
                "MATCH (p:ProcessedMap) WHERE p.snapshot_id = $s RETURN count(p) AS n",
                s=snapshot_id,
            ).single()["n"]
        driver.close()
        print("\nConstraint graph (Neo4j, global counts)")
        print(f"  :Block nodes:       {nodes}")
        print(f"  :ADJACENT_TO edges: {edges}")
        print(f"  :ProcessedMap for this snapshot: {processed}")
        if labels:
            print(f"  validity labels among edges touched by {snapshot_id}:")
            for row in labels:
                print(f"    {row['l']}: {row['n']}")
    except Exception as exc:  # noqa: BLE001
        print(f"\nConstraint graph: unreachable ({exc})")

    print("\nStage runs (last 10 touching this snapshot)")
    for status, summary in stage_runs:
        summary = (summary or "")[:120]
        print(f"  status={status}  summary={summary}")

    print("\nMilestone A kill-switch reference (docs/evaluation-plan.md)")
    if total:
        print(f"  ingestion success (artifact downloaded) = {_pct(downloaded, total)}  (floor 80%)")
        print(f"  parse success (parse_status=success)     = {_pct(parsed, total)}   (informal; parse-rate gate)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: scale_summary.py <snapshot_id>", file=sys.stderr)
        sys.exit(2)
    summarize(sys.argv[1])
