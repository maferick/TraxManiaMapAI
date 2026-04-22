"""Top-level CLI entrypoint. See ``src/cli/__init__.py``."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.ingestion import (
    ArtifactStore,
    HttpClient,
    MapIngestor,
    ReplayIngestor,
    ResponseCache,
    TmxClient,
    TokenBucket,
    close_stage_run,
    ensure_snapshot,
    open_stage_run,
)
from src.replay import (
    CohortAssignmentConfig,
    CohortAssignmentPipeline,
    FileBreadcrumbLoader,
    FileTelemetryLoader,
    ReplayCleanPipeline,
    default_breadcrumb_rules,
    default_rules,
)
from src.benchmarks.manifest import load as load_benchmark
from src.constraints import ConstraintGraphPipeline
from src.parsers import MapParsePipeline, ReplayParsePipeline, SubprocessParser
from src.evaluation import (
    AdjacencyGraphEvaluator,
    BehaviorProfileEvaluator,
    Evaluator,
    RouteCoverageEvaluator,
    StructuralEvaluator,
)
from src.evaluation.dryrun import DryRunRunner, render_markdown
from src.route import RouteExtractor, RoutePipeline
from src.route import create as create_clusterer
from src.schema.replays import ReplayCohort
from src.storage.mariadb import MigrationError, migrate, open_connection
from src.storage.neo4j_adapter import (
    Neo4jMigrationError,
    migrate as neo4j_migrate,
    open_driver,
)
from src.utils.config import code_version, load_config, resolve_config_hash

_LOG = logging.getLogger("src.cli")
_INGEST_STAGE = "ingest_maps"
_INGEST_STAGE_VERSION = "0.1.0"


def _cmd_migrate(args: argparse.Namespace) -> int:
    try:
        applied = migrate(config_path=args.config)
    except MigrationError as exc:
        _LOG.error("migration failed: %s", exc)
        return 1
    if applied:
        _LOG.info("applied %d migration(s): %s", len(applied), ", ".join(applied))
    else:
        _LOG.info("schema already up to date")
    return 0


def _cmd_neo4j_migrate(args: argparse.Namespace) -> int:
    try:
        applied = neo4j_migrate(config_path=args.config)
    except Neo4jMigrationError as exc:
        _LOG.error("neo4j migration failed: %s", exc)
        return 1
    if applied:
        _LOG.info(
            "applied %d neo4j migration(s): %s", len(applied), ", ".join(applied)
        )
    else:
        _LOG.info("neo4j schema already up to date")
    return 0


_PARSE_STAGE = "parse_maps"
_PARSE_STAGE_VERSION = "0.1.0"
_PARSE_REPLAYS_STAGE = "parse_replays"


def _cmd_parse_replays(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    gbx_cfg = (config.get("parsers") or {}).get("gbx") or {}
    executable = Path(
        args.parser_executable
        or gbx_cfg.get("executable")
        or "./parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper"
    )
    timeout = float(gbx_cfg.get("timeout_seconds", 30.0))
    parser_version = args.parser_version
    sha = code_version()
    config_hash = resolve_config_hash(config)
    conn = open_connection(config)
    parser = SubprocessParser(
        executable=executable,
        parser_version=parser_version,
        timeout_seconds=timeout,
    )
    try:
        input_ref = (
            f"snapshot={args.snapshot or 'ALL'};retry_transient={args.retry_transient}"
        )
        stage_run_id = open_stage_run(
            conn,
            stage=_PARSE_REPLAYS_STAGE,
            stage_version=_PARSE_STAGE_VERSION,
            resolved_config_hash=config_hash,
            code_version=sha,
            input_ref=input_ref,
        )
        pipeline = ReplayParsePipeline(conn=conn, parser=parser)
        try:
            stats = pipeline.run(
                snapshot_id=args.snapshot,
                max_replays=args.limit,
                retry_transient=args.retry_transient,
            )
        except Exception as exc:  # noqa: BLE001
            close_stage_run(
                conn, stage_run_id, status="failed", output_summary=None,
                error_taxonomy_code="unhandled", error_message=str(exc)[:2000],
            )
            _LOG.exception("parse-replays crashed")
            return 1
        status = "partial" if stats.errors else "success"
        close_stage_run(
            conn, stage_run_id, status=status, output_summary=stats.to_summary_json()
        )
        _LOG.info(
            "parse-replays %s: seen=%d parsed=%d transient=%d permanent=%d sidecars=%d",
            status,
            stats.replays_seen,
            stats.replays_parsed,
            stats.replays_failed_transient,
            stats.replays_failed_permanent,
            stats.sidecars_written,
        )
        return 0 if status == "success" else 1
    finally:
        conn.close()


def _cmd_parse_maps(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    gbx_cfg = (config.get("parsers") or {}).get("gbx") or {}

    executable = Path(
        args.parser_executable
        or gbx_cfg.get("executable")
        or "./parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper"
    )
    timeout = float(gbx_cfg.get("timeout_seconds", 30.0))
    parser_version = args.parser_version

    sha = code_version()
    config_hash = resolve_config_hash(config)
    conn = open_connection(config)
    parser = SubprocessParser(
        executable=executable,
        parser_version=parser_version,
        timeout_seconds=timeout,
    )
    try:
        input_ref = (
            f"snapshot={args.snapshot or 'ALL'};"
            f"retry_transient={args.retry_transient};"
            f"parser_version={parser_version}"
        )
        stage_run_id = open_stage_run(
            conn,
            stage=_PARSE_STAGE,
            stage_version=_PARSE_STAGE_VERSION,
            resolved_config_hash=config_hash,
            code_version=sha,
            input_ref=input_ref,
        )
        pipeline = MapParsePipeline(
            conn=conn,
            parser=parser,
            parser_version=parser_version,
            created_by_version=_PARSE_STAGE_VERSION,
        )
        try:
            stats = pipeline.run(
                snapshot_id=args.snapshot,
                max_maps=args.limit,
                retry_transient=args.retry_transient,
            )
        except Exception as exc:  # noqa: BLE001
            close_stage_run(
                conn,
                stage_run_id,
                status="failed",
                output_summary=None,
                error_taxonomy_code="unhandled",
                error_message=str(exc)[:2000],
            )
            _LOG.exception("parse-maps crashed")
            return 1
        status = "partial" if stats.errors else "success"
        close_stage_run(
            conn, stage_run_id, status=status, output_summary=stats.to_summary_json()
        )
        _LOG.info(
            "parse-maps %s: seen=%d parsed=%d transient=%d permanent=%d "
            "blocks=%d (grid=%d free=%d)",
            status,
            stats.maps_seen,
            stats.maps_parsed,
            stats.maps_failed_transient,
            stats.maps_failed_permanent,
            stats.total_blocks_written,
            stats.grid_blocks_written,
            stats.free_blocks_written,
        )
        return 0 if status == "success" else 1
    finally:
        conn.close()


_GRAPH_STAGE = "build_graph"


def _cmd_label_traversability(args: argparse.Namespace) -> int:
    from src.corridor.traversability import TraversabilityLabeler
    config = load_config(args.config)
    driver = open_driver(config)
    try:
        labeler = TraversabilityLabeler(driver, batch_size=int(args.batch_size))
        stats = labeler.run()
    finally:
        driver.close()
    _LOG.info(
        "label-traversability: edges=%d seed_valid=%d unsupported=%d unknown=%d "
        "unsupported_fraction=%.4f suppression_fraction=%.4f",
        stats.edges_seen,
        stats.seed_valid,
        stats.unsupported,
        stats.unknown,
        stats.unsupported_fraction,
        stats.suppression_fraction,
    )
    return 0


def _cmd_enumerate_corridors(args: argparse.Namespace) -> int:
    import json as _json
    from src.corridor.traversability import (
        DECO_ADJACENT_CONTAMINATION_CAP,
        MEDIAN_PATH_COUNT_CAP,
        P95_PATH_COUNT_CAP,
        VALIDATION_MAP_IDS_V1,
        VALIDATION_MAP_IDS_V2,
        enumerate_set,
    )
    config = load_config(args.config)
    ids: tuple[int, ...]
    if args.map_ids:
        ids = tuple(int(m) for m in args.map_ids)
    elif args.set == "v1":
        ids = VALIDATION_MAP_IDS_V1
    else:
        ids = VALIDATION_MAP_IDS_V2
    conn = open_connection(config)
    try:
        report = enumerate_set(conn, ids, depth_cap=args.depth_cap)
    finally:
        conn.close()

    # Per-map summary — one line per map with interval count + worst metrics.
    for mid, intervals in sorted(report.per_map.items()):
        if not intervals:
            _LOG.info("  map=%5d: no enumeration (missing anchors or placements)", mid)
            continue
        max_paths = max(iv.path_count for iv in intervals)
        worst_deco = max(iv.deco_adjacent_contamination for iv in intervals)
        unsupported = sum(iv.unsupported_edges_in_corridors for iv in intervals)
        non_drivable = sum(iv.non_drivable_cells_in_corridors for iv in intervals)
        _LOG.info(
            "  map=%5d intervals=%2d max_paths=%5d worst_deco=%.3f "
            "unsupported=%d non_drivable=%d",
            mid, len(intervals), max_paths, worst_deco, unsupported, non_drivable,
        )

    # Aggregate + gate summary.
    _LOG.info(
        "enumerate-corridors: maps=%d intervals=%d "
        "median_paths=%.0f p95_paths=%d",
        len(report.per_map),
        len(report.all_intervals()),
        report.median_path_count,
        report.p95_path_count,
    )
    gates = [
        ("§8.3.1 unsupported-free", report.passes_83_unsupported),
        ("§8.3.2 non-drivable-free", report.passes_83_non_drivable),
        (f"§8.3.3 deco-adjacent ≤ {DECO_ADJACENT_CONTAMINATION_CAP}",
         report.passes_83_deco_adjacent),
        ("§8.3.4 path stability", report.passes_83_stable),
        (f"§8.4 median ≤ {MEDIAN_PATH_COUNT_CAP}", report.passes_84_median),
        (f"§8.4 p95 ≤ {P95_PATH_COUNT_CAP}", report.passes_84_p95),
    ]
    all_passing = True
    for name, ok in gates:
        mark = "PASS" if ok else "FAIL"
        getattr(_LOG, "info" if ok else "warning")("GATE %s: %s", mark, name)
        if not ok:
            all_passing = False

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            _json.dumps(report.to_summary_json(), indent=2), encoding="utf-8"
        )
        _LOG.info("wrote JSON report: %s", args.json)
    return 0 if all_passing else 1


def _cmd_validate_traversability(args: argparse.Namespace) -> int:
    import json as _json
    from src.corridor.traversability import (
        VALIDATION_MAP_IDS,
        VALIDATION_MAP_IDS_V1,
        VALIDATION_MAP_IDS_V2,
        validate_set,
    )
    config = load_config(args.config)
    ids: tuple[int, ...]
    if args.map_ids:
        ids = tuple(int(m) for m in args.map_ids)
    elif args.set == "v1":
        ids = VALIDATION_MAP_IDS_V1
    elif args.set == "v2":
        ids = VALIDATION_MAP_IDS_V2
    else:
        ids = VALIDATION_MAP_IDS
    conn = open_connection(config)
    try:
        report = validate_set(
            conn, map_ids=ids, use_observations=args.use_observations
        )
    finally:
        conn.close()

    # Per-map lines first so the full picture is visible even when the
    # overall fails. Overall summary last.
    for m in report.per_map:
        obs_suffix = (
            f" obs={m.observations_applied}/{m.observations_available} "
            f"seed_only={m.anchor_sets_reachable_seed_only}"
            if args.use_observations else ""
        )
        _LOG.info(
            "  map=%5d cells=%6d edges=%6d (sv=%d us=%d uk=%d) "
            "anchors=%d/%d reach=%.3f unsup=%.3f%s%s",
            m.map_id,
            m.total_cells,
            m.total_edges,
            m.seed_valid_edges,
            m.unsupported_edges,
            m.unknown_edges,
            m.anchor_sets_reachable,
            m.anchor_sets_total,
            m.reachability_fraction,
            m.unsupported_fraction,
            obs_suffix,
            f" errors={','.join(m.errors)}" if m.errors else "",
        )
    _LOG.info(
        "validate-traversability: maps=%d maps_passing=%d "
        "intervals=%d/%d (%.3f) "
        "weighted_unsupported=%.3f weighted_suppression=%.3f",
        report.maps_total,
        report.maps_passing_reachability,
        report.intervals_reachable,
        report.intervals_total,
        report.interval_reachability_fraction,
        report.weighted_unsupported_fraction,
        report.weighted_suppression_fraction,
    )
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            _json.dumps(report.to_summary_json(), indent=2), encoding="utf-8"
        )
        _LOG.info("wrote JSON report: %s", args.json)

    # §8 commit-bar exit-code signal — 0 on pass, 1 on fail. Lets
    # CI/CD or manual runners treat this as a gate without re-parsing
    # the log line.
    passes_reachability = report.interval_reachability_fraction >= args.min_reachability
    passes_suppression = report.weighted_unsupported_fraction >= args.min_unsupported
    if not passes_reachability:
        _LOG.warning(
            "GATE FAIL: interval reachability %.3f below threshold %.3f",
            report.interval_reachability_fraction, args.min_reachability,
        )
    if not passes_suppression:
        _LOG.warning(
            "GATE FAIL: unsupported fraction %.3f below threshold %.3f",
            report.weighted_unsupported_fraction, args.min_unsupported,
        )
    return 0 if (passes_reachability and passes_suppression) else 1


def _cmd_build_graph(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    constraints_cfg = config.get("constraints", {}) or {}
    stage_version = str(constraints_cfg.get("stage_version", "0.1.0"))

    bench_ids = set(int(x) for x in constraints_cfg.get("benchmark_strong_map_ids", []) or [])
    broken_ids = set(int(x) for x in constraints_cfg.get("broken_fixture_map_ids", []) or [])

    sha = code_version()
    config_hash = resolve_config_hash(config)
    conn = open_connection(config)
    driver = open_driver(config)

    try:
        input_ref = (
            f"snapshot={args.snapshot or 'ALL'};"
            f"maps={'ALL' if not args.map_ids else ','.join(str(m) for m in args.map_ids)}"
        )
        stage_run_id = open_stage_run(
            conn,
            stage=_GRAPH_STAGE,
            stage_version=stage_version,
            resolved_config_hash=config_hash,
            code_version=sha,
            input_ref=input_ref,
        )
        pipeline = ConstraintGraphPipeline(
            mariadb=conn,
            neo4j_driver=driver,
            stage_version=stage_version,
            benchmark_strong_map_ids=bench_ids,
            broken_fixture_map_ids=broken_ids,
        )
        try:
            stats = pipeline.run(
                snapshot_id=args.snapshot,
                map_ids=args.map_ids or None,
                parser_version=args.parser_version,
            )
        except Exception as exc:  # noqa: BLE001
            close_stage_run(
                conn,
                stage_run_id,
                status="failed",
                output_summary=None,
                error_taxonomy_code="unhandled",
                error_message=str(exc)[:2000],
            )
            _LOG.exception("build-graph crashed")
            return 1
        status = "partial" if stats.errors else "success"
        close_stage_run(
            conn, stage_run_id, status=status, output_summary=stats.to_summary_json()
        )
        _LOG.info(
            "build-graph %s: seen=%d processed=%d skipped_already=%d "
            "no_placements=%d nodes=%d edges=%d",
            status,
            stats.maps_seen,
            stats.maps_processed,
            stats.maps_skipped_already_processed,
            stats.maps_skipped_no_placements,
            stats.nodes_merged,
            stats.edges_merged,
        )
        return 0 if status == "success" else 1
    finally:
        driver.close()
        conn.close()


def _cmd_ingest_maps(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    tmx_cfg = config.get("ingestion", {}).get("tmx", {})
    snapshot_cfg = config.get("ingestion", {}).get("snapshot", {})
    artifacts_cfg = config.get("storage", {}).get("artifacts", {})

    snapshot_id = args.snapshot or snapshot_cfg.get("id")
    if not snapshot_id:
        _LOG.error("snapshot id required: pass --snapshot or set ingestion.snapshot.id")
        return 2

    rate = float(tmx_cfg.get("requests_per_second", 1.0))
    user_agent = tmx_cfg.get("user_agent")
    base_url = tmx_cfg.get("base_url")
    cache_dir = Path(tmx_cfg.get("cache_dir", "./data/cache/tmx"))
    artifacts_root = Path(artifacts_cfg.get("root", "./data/artifacts"))
    retry_cfg = tmx_cfg.get("retry", {}) or {}
    backoff = tuple(float(s) for s in retry_cfg.get("backoff_seconds", (2, 4, 8, 16)))
    max_total_retry = float(retry_cfg.get("max_total_retry_seconds", 120.0))
    timeout = float(tmx_cfg.get("timeout_seconds", 30.0))

    config_hash = resolve_config_hash(config)
    sha = code_version()

    http = HttpClient(
        base_url=base_url,
        user_agent=user_agent,
        rate_limiter=TokenBucket(rate_per_second=rate),
        cache=ResponseCache(cache_dir),
        backoff_seconds=backoff,
        timeout_seconds=timeout,
        max_total_retry_seconds=max_total_retry,
    )
    tmx_client = TmxClient(http)
    store = ArtifactStore(artifacts_root)
    conn = open_connection(config)

    try:
        ensure_snapshot(
            conn,
            snapshot_id=snapshot_id,
            source_system="tmx",
            user_agent=user_agent,
            rate_limit_rps=rate,
            resolved_config_hash=config_hash,
            code_version=sha,
        )
        stage_run_id = open_stage_run(
            conn,
            stage=_INGEST_STAGE,
            stage_version=_INGEST_STAGE_VERSION,
            resolved_config_hash=config_hash,
            code_version=sha,
            input_ref=f"snapshot={snapshot_id}",
        )
        ingestor = MapIngestor(
            tmx=tmx_client,
            conn=conn,
            artifact_store=store,
            snapshot_id=snapshot_id,
            parser_version=args.parser_version,
            created_by_version=_INGEST_STAGE_VERSION,
            max_maps=args.limit,
            download_artifacts=not args.no_download_artifacts,
            random_count=args.random,
        )
        try:
            stats = ingestor.run()
        except Exception as exc:  # noqa: BLE001
            close_stage_run(
                conn,
                stage_run_id,
                status="failed",
                output_summary=None,
                error_taxonomy_code="unhandled",
                error_message=str(exc)[:2000],
            )
            _LOG.exception("ingestion crashed")
            return 1
        status = "failed" if stats.errors and stats.maps_inserted == 0 else (
            "partial" if stats.errors else "success"
        )
        close_stage_run(
            conn,
            stage_run_id,
            status=status,
            output_summary=stats.to_summary_json(),
        )
        _LOG.info(
            "ingest_maps %s: seen=%d inserted=%d updated=%d "
            "artifacts=%d artifact_failures=%d errors=%d",
            status,
            stats.maps_seen,
            stats.maps_inserted,
            stats.maps_updated,
            stats.artifacts_downloaded,
            stats.artifacts_failed,
            len(stats.errors),
        )
        return 0 if status == "success" else 1
    finally:
        conn.close()


_INGEST_REPLAYS_STAGE = "ingest_replays"
_INGEST_REPLAYS_STAGE_VERSION = "0.1.0"


def _resolve_target_maps(conn, args) -> list[tuple[int, str]]:
    """Return [(db_map_id, source_map_id)] based on the CLI flags.

    Resolution precedence: explicit --map-id list, else the snapshot's
    parsed-successfully maps limited by --max-maps (or all if unset).
    """
    if args.map_ids:
        from src.storage.mariadb import cursor
        placeholders = ",".join(["%s"] * len(args.map_ids))
        with cursor(conn) as cur:
            cur.execute(
                f"SELECT id, source_map_id FROM maps WHERE id IN ({placeholders})",
                tuple(args.map_ids),
            )
            return [(int(r[0]), str(r[1])) for r in cur.fetchall()]
    from src.storage.mariadb import cursor
    sql = "SELECT id, source_map_id FROM maps WHERE parse_status = 'success'"
    params: list = []
    if args.snapshot:
        sql += " AND ingestion_snapshot = %s"
        params.append(args.snapshot)
    if args.top_awards is not None:
        sql += " ORDER BY award_count DESC"
    else:
        sql += " ORDER BY id"
    if args.max_maps is not None or args.top_awards is not None:
        limit = args.max_maps if args.max_maps is not None else args.top_awards
        sql += " LIMIT %s"
        params.append(int(limit))
    with cursor(conn) as cur:
        cur.execute(sql, tuple(params))
        return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


def _cmd_ingest_replays(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    tmx_cfg = config.get("ingestion", {}).get("tmx", {})
    artifacts_cfg = config.get("storage", {}).get("artifacts", {})

    snapshot_id = args.snapshot or config.get("ingestion", {}).get("snapshot", {}).get("id")
    if not snapshot_id:
        _LOG.error("--snapshot required (or set ingestion.snapshot.id in config)")
        return 2

    rate = float(tmx_cfg.get("requests_per_second", 1.0))
    user_agent = tmx_cfg.get("user_agent")
    base_url = tmx_cfg.get("base_url")
    cache_dir = Path(tmx_cfg.get("cache_dir", "./data/cache/tmx"))
    artifacts_root = Path(artifacts_cfg.get("root", "./data/artifacts"))
    retry_cfg = tmx_cfg.get("retry", {}) or {}
    backoff = tuple(float(s) for s in retry_cfg.get("backoff_seconds", (2, 4, 8, 16)))
    max_total_retry = float(retry_cfg.get("max_total_retry_seconds", 120.0))
    timeout = float(tmx_cfg.get("timeout_seconds", 30.0))

    config_hash = resolve_config_hash(config)
    sha = code_version()

    http = HttpClient(
        base_url=base_url,
        user_agent=user_agent,
        rate_limiter=TokenBucket(rate_per_second=rate),
        cache=ResponseCache(cache_dir),
        backoff_seconds=backoff,
        timeout_seconds=timeout,
        max_total_retry_seconds=max_total_retry,
    )
    tmx_client = TmxClient(http)
    store = ArtifactStore(artifacts_root)
    conn = open_connection(config)
    try:
        map_refs = _resolve_target_maps(conn, args)
        if not map_refs:
            _LOG.error("no target maps matched the given filters")
            return 1
        _LOG.info("ingest-replays: targeting %d maps", len(map_refs))

        ensure_snapshot(
            conn,
            snapshot_id=snapshot_id,
            source_system="tmx",
            user_agent=user_agent,
            rate_limit_rps=rate,
            resolved_config_hash=config_hash,
            code_version=sha,
        )
        stage_run_id = open_stage_run(
            conn,
            stage=_INGEST_REPLAYS_STAGE,
            stage_version=_INGEST_REPLAYS_STAGE_VERSION,
            resolved_config_hash=config_hash,
            code_version=sha,
            input_ref=(
                f"snapshot={snapshot_id};maps={len(map_refs)};"
                f"per_map={args.per_map}"
            ),
        )
        ingestor = ReplayIngestor(
            tmx=tmx_client,
            conn=conn,
            artifact_store=store,
            snapshot_id=snapshot_id,
            created_by_version=_INGEST_REPLAYS_STAGE_VERSION,
            per_map=args.per_map,
            download_artifacts=not args.no_download_artifacts,
        )
        try:
            stats = ingestor.run(map_refs)
        except Exception as exc:  # noqa: BLE001
            close_stage_run(
                conn,
                stage_run_id,
                status="failed",
                output_summary=None,
                error_taxonomy_code="unhandled",
                error_message=str(exc)[:2000],
            )
            _LOG.exception("ingest-replays crashed")
            return 1
        status = "partial" if stats.errors or stats.artifacts_failed else "success"
        close_stage_run(
            conn,
            stage_run_id,
            status=status,
            output_summary=stats.to_summary_json(),
        )
        _LOG.info(
            "ingest-replays %s: maps_seen=%d no_replays=%d "
            "replays_seen=%d inserted=%d updated=%d "
            "artifacts=%d artifact_failures=%d errors=%d",
            status,
            stats.maps_seen,
            stats.maps_with_no_replays,
            stats.replays_seen,
            stats.replays_inserted,
            stats.replays_updated,
            stats.artifacts_downloaded,
            stats.artifacts_failed,
            len(stats.errors),
        )
        return 0 if status == "success" else 1
    finally:
        conn.close()


_CLEAN_STAGE = "replay_clean"
_COHORT_STAGE = "assign_cohorts"


def _cmd_replay_clean(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    rc_cfg = config.get("replay_cleaning", {}) or {}
    clean_version = str(rc_cfg.get("stage_version", "0.1.0"))
    thresholds = rc_cfg.get("rules", {}) or {}

    sha = code_version()
    config_hash = resolve_config_hash(config)
    conn = open_connection(config)
    try:
        stage_run_id = open_stage_run(
            conn,
            stage=_CLEAN_STAGE,
            stage_version=clean_version,
            resolved_config_hash=config_hash,
            code_version=sha,
            input_ref=f"snapshot={args.snapshot or 'ALL'}",
        )
        pipeline = ReplayCleanPipeline(
            conn=conn,
            loader=FileTelemetryLoader(),
            rules=default_rules(),
            thresholds_by_rule=thresholds,
            clean_version=clean_version,
            breadcrumb_loader=FileBreadcrumbLoader(),
            breadcrumb_rules=default_breadcrumb_rules(),
        )
        try:
            stats = pipeline.run(
                snapshot_id=args.snapshot,
                max_replays=args.limit,
            )
        except Exception as exc:  # noqa: BLE001
            close_stage_run(
                conn,
                stage_run_id,
                status="failed",
                output_summary=None,
                error_taxonomy_code="unhandled",
                error_message=str(exc)[:2000],
            )
            _LOG.exception("replay-clean crashed")
            return 1
        has_errors = bool(stats.errors or stats.load_failures or stats.rule_exceptions)
        status = "partial" if has_errors else "success"
        close_stage_run(
            conn, stage_run_id, status=status, output_summary=stats.to_summary_json()
        )
        _LOG.info(
            "replay-clean %s: seen=%d clean=%d warn=%d rejected=%d "
            "load_fail=%d breadcrumb_path_used=%d",
            status,
            stats.replays_seen,
            stats.replays_clean,
            stats.replays_usable_with_warnings,
            stats.replays_rejected,
            stats.load_failures,
            stats.breadcrumb_path_used,
        )
        return 0 if status == "success" else 1
    finally:
        conn.close()


_ROUTE_STAGE = "extract_route"
_EVAL_STAGE = "eval_benchmark"


def _build_evaluator_stack(
    conn, driver, *, names: list[str], parser_version: str
) -> list[Evaluator]:
    stack: list[Evaluator] = []
    for name in names:
        if name == "structural":
            stack.append(StructuralEvaluator(conn, parser_version=parser_version))
        elif name == "adjacency_graph":
            stack.append(
                AdjacencyGraphEvaluator(conn, driver, parser_version=parser_version)
            )
        elif name == "route_coverage":
            stack.append(RouteCoverageEvaluator(conn))
        elif name == "behavior_profile":
            stack.append(BehaviorProfileEvaluator(conn))
        else:
            raise ValueError(f"unknown evaluator name: {name!r}")
    return stack


def _cmd_eval_benchmark(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    dryrun_cfg = (config.get("evaluation") or {}).get("dryrun") or {}

    stage_version = str(dryrun_cfg.get("stage_version", "0.1.0"))
    evaluator_names = args.evaluators or list(
        dryrun_cfg.get("evaluators", ["structural", "adjacency_graph", "route_coverage"])
    )
    parser_version = str(dryrun_cfg.get("parser_version", "0.0.0"))
    community_sample_size = (
        args.community_sample_size
        if args.community_sample_size is not None
        else int(dryrun_cfg.get("community_sample_size", 0))
    )
    community_snapshot = args.snapshot or dryrun_cfg.get("community_snapshot_id") or None
    manifest_paths = args.benchmark_manifests or list(
        dryrun_cfg.get("benchmark_manifests", [])
    )
    report_path = Path(
        args.report or dryrun_cfg.get("report_path", "reports/evaluator-dryrun-v1.md")
    )

    manifests = [load_benchmark(Path(p)) for p in manifest_paths]

    sha = code_version()
    config_hash = resolve_config_hash(config)
    conn = open_connection(config)
    driver = open_driver(config) if "adjacency_graph" in evaluator_names else None

    try:
        stack = _build_evaluator_stack(
            conn, driver, names=evaluator_names, parser_version=parser_version
        )
        input_ref = (
            f"manifests={len(manifests)};"
            f"community={community_sample_size};"
            f"snapshot={community_snapshot or 'ALL'}"
        )
        stage_run_id = open_stage_run(
            conn,
            stage=_EVAL_STAGE,
            stage_version=stage_version,
            resolved_config_hash=config_hash,
            code_version=sha,
            input_ref=input_ref,
        )
        runner = DryRunRunner(
            conn=conn,
            evaluators=stack,
            benchmark_manifests=manifests,
            community_sample_size=community_sample_size,
            community_snapshot_id=community_snapshot,
            stage_version=stage_version,
        )
        try:
            report = runner.run()
        except Exception as exc:  # noqa: BLE001
            close_stage_run(
                conn,
                stage_run_id,
                status="failed",
                output_summary=None,
                error_taxonomy_code="unhandled",
                error_message=str(exc)[:2000],
            )
            _LOG.exception("eval-benchmark crashed")
            return 1

        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_markdown(report), encoding="utf-8")

        status = "partial" if report.errors else "success"
        close_stage_run(
            conn,
            stage_run_id,
            status=status,
            output_summary=report.to_summary_json(),
        )
        _LOG.info(
            "eval-benchmark %s: maps=%d results=%d report=%s",
            status,
            len(report.maps),
            sum(len(v) for v in report.results.values()),
            report_path,
        )
        return 0 if status == "success" else 1
    finally:
        if driver is not None:
            driver.close()
        conn.close()


def _cmd_extract_route(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    ri_cfg = config.get("route_inference", {}) or {}
    artifacts_cfg = config.get("storage", {}).get("artifacts", {}) or {}

    stage_version = str(ri_cfg.get("stage_version", "0.1.0"))
    route_version = args.route_version or str(ri_cfg.get("route_version", "0.1.0"))
    clusterer_name = args.clusterer or str(ri_cfg.get("clusterer", "grid"))
    clusterer_params = ri_cfg.get("clusterer_params") or {}
    cohort_name = args.cohort or str(ri_cfg.get("cohort", "intent"))
    artifacts_root = Path(artifacts_cfg.get("root", "./data/artifacts"))

    clusterer = create_clusterer(clusterer_name, clusterer_params)
    extractor = RouteExtractor(
        clusterer=clusterer,
        n_centerline_points=int(ri_cfg.get("n_centerline_points", 200)),
        refinement_window_m=float(ri_cfg.get("refinement_window_m", 5.0)),
        branch_bin_size_m=float(ri_cfg.get("branch_bin_size_m", 10.0)),
        branch_min_samples_per_cluster=int(
            ri_cfg.get("branch_min_samples_per_cluster", 3)
        ),
    )

    sha = code_version()
    config_hash = resolve_config_hash(config)
    conn = open_connection(config)
    try:
        input_ref = (
            f"snapshot={args.snapshot or 'ALL'};"
            f"maps={'ALL' if not args.map_ids else ','.join(str(m) for m in args.map_ids)}"
        )
        stage_run_id = open_stage_run(
            conn,
            stage=_ROUTE_STAGE,
            stage_version=stage_version,
            resolved_config_hash=config_hash,
            code_version=sha,
            input_ref=input_ref,
        )
        pipeline = RoutePipeline(
            conn=conn,
            loader=FileTelemetryLoader(),
            extractor=extractor,
            artifacts_root=artifacts_root,
            route_version=route_version,
            created_by_version=stage_version,
            clustering_method=clusterer_name,
            clustering_params=clusterer_params,
            cohort=ReplayCohort(cohort_name),
            min_replays_per_map=int(ri_cfg.get("min_replays_per_map", 3)),
        )
        try:
            stats = pipeline.run(
                snapshot_id=args.snapshot,
                map_ids=args.map_ids or None,
            )
        except Exception as exc:  # noqa: BLE001
            close_stage_run(
                conn,
                stage_run_id,
                status="failed",
                output_summary=None,
                error_taxonomy_code="unhandled",
                error_message=str(exc)[:2000],
            )
            _LOG.exception("extract-route crashed")
            return 1
        status = (
            "partial"
            if stats.errors or stats.routes_failed
            else ("success" if stats.routes_written or stats.maps_seen == 0 else "partial")
        )
        close_stage_run(
            conn, stage_run_id, status=status, output_summary=stats.to_summary_json()
        )
        _LOG.info(
            "extract-route %s: maps_seen=%d written=%d skipped=%d failed=%d",
            status,
            stats.maps_seen,
            stats.routes_written,
            stats.routes_skipped_existing,
            stats.routes_failed,
        )
        return 0 if status == "success" else 1
    finally:
        conn.close()


def _cohort_config_from(config: dict) -> CohortAssignmentConfig:
    raw = config.get("cohorts") or {}
    kwargs = {}
    for key in (
        "intent_lower_pct",
        "intent_upper_pct",
        "performance_top_pct",
        "robustness_lower_pct",
        "robustness_upper_pct",
    ):
        if key in raw:
            kwargs[key] = float(raw[key])
    if "small_map_n" in raw:
        kwargs["small_map_n"] = int(raw["small_map_n"])
    return CohortAssignmentConfig(**kwargs) if kwargs else CohortAssignmentConfig()


def _cmd_assign_cohorts(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    rc_cfg = config.get("replay_cleaning", {}) or {}
    stage_version = str(rc_cfg.get("stage_version", "0.1.0"))

    sha = code_version()
    config_hash = resolve_config_hash(config)
    conn = open_connection(config)
    cohort_cfg = _cohort_config_from(config)

    try:
        stage_run_id = open_stage_run(
            conn,
            stage=_COHORT_STAGE,
            stage_version=stage_version,
            resolved_config_hash=config_hash,
            code_version=sha,
            input_ref=f"snapshot={args.snapshot or 'ALL'}",
        )
        pipeline = CohortAssignmentPipeline(conn=conn, config=cohort_cfg)
        try:
            stats = pipeline.run(snapshot_id=args.snapshot)
        except Exception as exc:  # noqa: BLE001
            close_stage_run(
                conn,
                stage_run_id,
                status="failed",
                output_summary=None,
                error_taxonomy_code="unhandled",
                error_message=str(exc)[:2000],
            )
            _LOG.exception("assign-cohorts crashed")
            return 1
        close_stage_run(
            conn, stage_run_id, status="success", output_summary=stats.to_summary_json()
        )
        _LOG.info(
            "assign-cohorts success: maps=%d replays=%d",
            stats.maps_processed,
            stats.replays_assigned,
        )
        return 0
    finally:
        conn.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="src.cli")
    parser.add_argument("--config", type=Path, default=None, help="path to settings.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    migrate_cmd = sub.add_parser("migrate", help="Apply pending MariaDB migrations")
    migrate_cmd.set_defaults(func=_cmd_migrate)

    ingest_maps_cmd = sub.add_parser(
        "ingest-maps", help="Run a TMX map-ingestion pass under a snapshot"
    )
    ingest_maps_cmd.add_argument("--snapshot", type=str, default=None)
    ingest_maps_cmd.add_argument("--limit", type=int, default=None)
    ingest_maps_cmd.add_argument("--parser-version", type=str, default="0.0.0")
    ingest_maps_cmd.add_argument("--no-download-artifacts", action="store_true")
    ingest_maps_cmd.add_argument(
        "--random",
        type=int,
        default=None,
        metavar="N",
        help="skip paginated listing; fetch N random maps (one API call per map)",
    )
    ingest_maps_cmd.set_defaults(func=_cmd_ingest_maps)

    ingest_replays_cmd = sub.add_parser(
        "ingest-replays",
        help="Fetch leaderboard replays for maps in a snapshot and download their .Replay.Gbx files",
    )
    ingest_replays_cmd.add_argument(
        "--snapshot", type=str, default=None,
        help="ingestion snapshot id (used to filter source maps AND tag the new replays)",
    )
    ingest_replays_cmd.add_argument(
        "--map-id", dest="map_ids", type=int, action="append", default=None,
        help="restrict to specific maps.id values (repeatable); overrides snapshot filter",
    )
    ingest_replays_cmd.add_argument(
        "--max-maps", type=int, default=None,
        help="cap the number of maps processed (useful for smoke tests)",
    )
    ingest_replays_cmd.add_argument(
        "--top-awards", type=int, default=None,
        help="pick top-N maps by award_count (overrides --max-maps; implies ordering)",
    )
    ingest_replays_cmd.add_argument(
        "--per-map", type=int, default=None, metavar="K",
        help="limit replays per map (TMX's list endpoint caps at 25 anyway)",
    )
    ingest_replays_cmd.add_argument(
        "--no-download-artifacts", action="store_true",
        help="skip .Replay.Gbx downloads; just record metadata",
    )
    ingest_replays_cmd.set_defaults(func=_cmd_ingest_replays)

    parse_maps_cmd = sub.add_parser(
        "parse-maps",
        help="Parse unparsed maps via the GBX wrapper; writes block_placements rows",
    )
    parse_maps_cmd.add_argument("--snapshot", type=str, default=None)
    parse_maps_cmd.add_argument("--limit", type=int, default=None)
    parse_maps_cmd.add_argument(
        "--parser-version", type=str, default="0.1.0",
        help="parser_version stamped on block_placements rows",
    )
    parse_maps_cmd.add_argument(
        "--parser-executable", type=str, default=None,
        help="override path to the GBX wrapper binary (config.parsers.gbx.executable otherwise)",
    )
    parse_maps_cmd.add_argument(
        "--retry-transient", action="store_true",
        help="re-parse maps whose previous attempt was failed_transient",
    )
    parse_maps_cmd.set_defaults(func=_cmd_parse_maps)

    parse_replays_cmd = sub.add_parser(
        "parse-replays",
        help="Parse unparsed replays via the GBX wrapper; writes telemetry.json sidecars",
    )
    parse_replays_cmd.add_argument("--snapshot", type=str, default=None)
    parse_replays_cmd.add_argument("--limit", type=int, default=None)
    parse_replays_cmd.add_argument("--parser-version", type=str, default="0.1.0")
    parse_replays_cmd.add_argument("--parser-executable", type=str, default=None)
    parse_replays_cmd.add_argument("--retry-transient", action="store_true")
    parse_replays_cmd.set_defaults(func=_cmd_parse_replays)

    replay_clean_cmd = sub.add_parser(
        "replay-clean", help="Classify unprocessed replays via the rule stack"
    )
    replay_clean_cmd.add_argument("--snapshot", type=str, default=None)
    replay_clean_cmd.add_argument("--limit", type=int, default=None)
    replay_clean_cmd.set_defaults(func=_cmd_replay_clean)

    assign_cohorts_cmd = sub.add_parser(
        "assign-cohorts", help="Compute per-map cohort membership over clean replays"
    )
    assign_cohorts_cmd.add_argument("--snapshot", type=str, default=None)
    assign_cohorts_cmd.set_defaults(func=_cmd_assign_cohorts)

    neo4j_migrate_cmd = sub.add_parser(
        "neo4j-migrate", help="Apply pending Neo4j Cypher migrations"
    )
    neo4j_migrate_cmd.set_defaults(func=_cmd_neo4j_migrate)

    build_graph_cmd = sub.add_parser(
        "build-graph", help="Build the constraint graph from block placements"
    )
    build_graph_cmd.add_argument("--snapshot", type=str, default=None)
    build_graph_cmd.add_argument(
        "--map-id",
        dest="map_ids",
        type=int,
        action="append",
        default=None,
        help="restrict to a specific map id (repeatable)",
    )
    build_graph_cmd.add_argument("--parser-version", type=str, default=None)
    build_graph_cmd.set_defaults(func=_cmd_build_graph)

    eval_benchmark_cmd = sub.add_parser(
        "eval-benchmark",
        help="Run the evaluator dry-run and render reports/evaluator-dryrun-v1.md",
    )
    eval_benchmark_cmd.add_argument(
        "--benchmark-manifest",
        dest="benchmark_manifests",
        type=str,
        action="append",
        default=None,
        help="path to a benchmark manifest (repeatable; overrides config)",
    )
    eval_benchmark_cmd.add_argument(
        "--evaluator",
        dest="evaluators",
        type=str,
        action="append",
        default=None,
        help="restrict to specific evaluators (repeatable)",
    )
    eval_benchmark_cmd.add_argument("--community-sample-size", type=int, default=None)
    eval_benchmark_cmd.add_argument("--snapshot", type=str, default=None)
    eval_benchmark_cmd.add_argument("--report", type=str, default=None)
    eval_benchmark_cmd.set_defaults(func=_cmd_eval_benchmark)

    # Audit commands live in their own module to keep this parser file
    # focused on principal pipeline stages.
    from src.cli.audit_commands import register_audit_commands
    register_audit_commands(sub)

    label_traversability_cmd = sub.add_parser(
        "label-traversability",
        help="Label ADJACENT_TO edges with a traversability state "
             "(seed_valid / unsupported / unknown)",
    )
    label_traversability_cmd.add_argument(
        "--batch-size", type=int, default=2000,
        help="UNWIND batch size for the per-edge property update (default 2000)",
    )
    label_traversability_cmd.set_defaults(func=_cmd_label_traversability)

    validate_traversability_cmd = sub.add_parser(
        "validate-traversability",
        help="Step 3 / §8 commit-bar: measure suppression + reachability "
             "on the fixed 10-map validation set. Exit 0 on pass, 1 on fail.",
    )
    validate_traversability_cmd.add_argument(
        "--map-id", dest="map_ids", type=int, action="append", default=None,
        help="override the default validation set (repeatable)",
    )
    validate_traversability_cmd.add_argument(
        "--set", choices=("v1", "v2"), default=None,
        help="pick one of the frozen sets: v1 (original, structural diversity), "
             "v2 (data-aware, replay-coverage-first). Default: current = v2",
    )
    validate_traversability_cmd.add_argument(
        "--min-reachability", type=float, default=0.90,
        help="§8.1 gate: min interval reachability fraction (default 0.90)",
    )
    validate_traversability_cmd.add_argument(
        "--min-unsupported", type=float, default=0.80,
        help="§8.2 gate: min weighted unsupported fraction (default 0.80)",
    )
    validate_traversability_cmd.add_argument(
        "--json", type=str, default=None,
        help="write machine-readable JSON report to this path",
    )
    validate_traversability_cmd.add_argument(
        "--use-observations", action="store_true",
        help="augment seed-valid BFS with replay-observed connectivity assertions "
             "(Phase 3 inductive layer — observations don't bump constraint-graph validity)",
    )
    validate_traversability_cmd.set_defaults(func=_cmd_validate_traversability)

    enumerate_corridors_cmd = sub.add_parser(
        "enumerate-corridors",
        help="Step 4 / §8.3 + §8.4 gates: enumerate corridor candidates per "
             "interval, run automated sanity checks. Exit 0 on all-pass, 1 on fail.",
    )
    enumerate_corridors_cmd.add_argument(
        "--map-id", dest="map_ids", type=int, action="append", default=None,
        help="override the default validation set (repeatable)",
    )
    enumerate_corridors_cmd.add_argument(
        "--set", choices=("v1", "v2"), default="v2",
        help="which frozen validation set to run against (default v2)",
    )
    enumerate_corridors_cmd.add_argument(
        "--depth-cap", type=int, default=10,
        help="max path depth for DFS enumeration (default 10)",
    )
    enumerate_corridors_cmd.add_argument(
        "--json", type=str, default=None,
        help="write machine-readable JSON report to this path",
    )
    enumerate_corridors_cmd.set_defaults(func=_cmd_enumerate_corridors)

    extract_route_cmd = sub.add_parser(
        "extract-route", help="Infer route artifacts from cohort-assigned replays"
    )
    extract_route_cmd.add_argument("--snapshot", type=str, default=None)
    extract_route_cmd.add_argument(
        "--map-id",
        dest="map_ids",
        type=int,
        action="append",
        default=None,
        help="restrict to a specific map id (repeatable)",
    )
    extract_route_cmd.add_argument(
        "--clusterer", type=str, default=None,
        help="grid | dbscan | per_segment (overrides config)",
    )
    extract_route_cmd.add_argument("--cohort", type=str, default=None)
    extract_route_cmd.add_argument("--route-version", type=str, default=None)
    extract_route_cmd.set_defaults(func=_cmd_extract_route)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
