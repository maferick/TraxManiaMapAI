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
    FileTelemetryLoader,
    ReplayCleanPipeline,
    default_rules,
)
from src.route import RouteExtractor, RoutePipeline
from src.route import create as create_clusterer
from src.schema.replays import ReplayCohort
from src.storage.mariadb import MigrationError, migrate, open_connection
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


def _cmd_ingest_replays(args: argparse.Namespace) -> int:
    _LOG.error(
        "ingest-replays is scaffolded but not wired to TMX endpoints yet. "
        "The replay adapter shape mirrors TmxClient; implementation lands once "
        "the replay endpoints are pinned."
    )
    return 2


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
            "replay-clean %s: seen=%d clean=%d warn=%d rejected=%d load_fail=%d",
            status,
            stats.replays_seen,
            stats.replays_clean,
            stats.replays_usable_with_warnings,
            stats.replays_rejected,
            stats.load_failures,
        )
        return 0 if status == "success" else 1
    finally:
        conn.close()


_ROUTE_STAGE = "extract_route"


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
    ingest_maps_cmd.set_defaults(func=_cmd_ingest_maps)

    ingest_replays_cmd = sub.add_parser("ingest-replays", help="(stub) replay ingestion")
    ingest_replays_cmd.set_defaults(func=_cmd_ingest_replays)

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
