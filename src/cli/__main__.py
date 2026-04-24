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
    CorridorConfidenceEvaluator,
    CorridorLearnedEvaluator,
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


def _cmd_build_traversability_evidence(args: argparse.Namespace) -> int:
    from src.corridor.traversability import build_set_evidence
    config = load_config(args.config)
    conn = open_connection(config)
    try:
        stats = build_set_evidence(
            conn,
            map_ids=args.map_ids,
            snapshot_id=args.snapshot,
            limit=args.limit,
        )
    finally:
        conn.close()
    _LOG.info(
        "build-traversability-evidence: maps_seen=%d maps_written=%d "
        "skipped=%d edges=%d (sv=%d us=%d uk=%d) errors=%d version=%s",
        stats.maps_seen, stats.maps_written, stats.maps_skipped_no_placements,
        stats.edges_written, stats.seed_valid, stats.unsupported,
        stats.unknown, len(stats.errors), stats.classification_version,
    )
    return 0 if not stats.errors else 1


def _cmd_update_path_support(args: argparse.Namespace) -> int:
    from src.corridor.traversability import update_path_support
    config = load_config(args.config)
    conn = open_connection(config)
    try:
        stats = update_path_support(
            conn, map_ids=args.map_ids,
            snapshot_id=args.snapshot, limit=args.limit,
        )
    finally:
        conn.close()
    _LOG.info(
        "update-path-support: maps_seen=%d updated=%d skipped=%d "
        "intervals=%d edges_updated=%d paths=%d errors=%d",
        stats.maps_seen, stats.maps_updated, stats.maps_skipped_no_evidence,
        stats.intervals_enumerated, stats.edges_updated,
        stats.path_count_total, len(stats.errors),
    )
    return 0 if not stats.errors else 1


def _cmd_generate_map(args: argparse.Namespace) -> int:
    import json as _json
    from src.generation import GenerationInputs, generate_from_base
    config = load_config(args.config)
    inputs = GenerationInputs(
        base_map_id=args.base_map_id,
        base_map_source_id=None,        # generator fills from DB
        style_tag_filter=args.style_tag_filter,
        difficulty=args.difficulty,
        random_seed=args.random_seed,
        strip=args.strip,
    )
    conn = open_connection(config)
    try:
        artifact = generate_from_base(conn, inputs=inputs, config=config)
    finally:
        conn.close()
    fin = artifact["finishability"]
    _LOG.info(
        "generate-map: run_id=%s base=%d route_verified=%s "
        "estimated_time_ms=%s ai_confidence=%s reject=%s",
        artifact["run_id"],
        inputs.base_map_id,
        fin["route_verified"],
        fin["estimated_time_ms"],
        fin["ai_confidence"],
        fin["reject_reason"],
    )
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_json.dumps(artifact, indent=2), encoding="utf-8")
        _LOG.info("wrote generated-map artifact: %s", args.output)
    return 0 if fin["route_verified"] else 1


def _cmd_generate_ai_map(args: argparse.Namespace) -> int:
    import json as _json
    from src.generation.ai_generator import (
        AIGenerationInputs,
        generate_ai_map,
    )
    config = load_config(args.config)
    inputs = AIGenerationInputs(
        base_map_id=args.base_map_id,
        random_seed=args.random_seed,
        style_tag_filter=args.style_tag_filter,
        difficulty=args.difficulty,
        beam_width=args.beam_width,
        max_interval_depth=args.max_interval_depth,
    )
    conn = open_connection(config)
    try:
        artifact = generate_ai_map(conn, inputs=inputs, config=config)
    finally:
        conn.close()
    fin = artifact["finishability"]
    _LOG.info(
        "generate-ai-map: run_id=%s base=%d synthesised=%d "
        "route_verified=%s ai_confidence=%s reject=%s",
        artifact["run_id"], inputs.base_map_id,
        len(artifact["map"]["blocks"]) - sum(
            1 for b in artifact["map"]["blocks"] if "ai_score" not in b
        ),
        fin["route_verified"], fin["ai_confidence"], fin["reject_reason"],
    )
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_json.dumps(artifact, indent=2), encoding="utf-8")
        _LOG.info("wrote ai-generated artifact: %s", args.output)
    return 0 if fin["route_verified"] else 1


def _cmd_remote_test_serve(args: argparse.Namespace) -> int:
    """Launch the Linux-side queue server (PR1 of the remote-test rig)."""
    from src.remote_test.server import create_app, resolve_auth_token
    from src.remote_test.storage import JobStore

    db_path = Path(args.db)
    artifacts_root = Path(args.artifacts_root)
    token = resolve_auth_token(args.token)
    store = JobStore(db_path)
    app = create_app(
        store=store, artifacts_root=artifacts_root,
        auth_token=token, allow_insecure=args.allow_insecure,
    )
    host = args.host
    port = int(args.port)
    _LOG.info(
        "remote-test-serve starting on %s:%d (db=%s artifacts=%s "
        "auth=%s)",
        host, port, db_path, artifacts_root,
        "disabled" if args.allow_insecure else "enabled",
    )
    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    finally:
        store.close()
    return 0


def _cmd_test_in_game(args: argparse.Namespace) -> int:
    """End-to-end: generate → emit → enqueue → poll → pretty-print.

    This is the closed-loop entry point for the remote-test rig:
    one command turns a base_map_id + seed into a queued job,
    watches the Windows agent process it, and prints the
    telemetry report.

    Exit codes:
      0 → job completed (telemetry received; route_verified may
          still be False — that's the map's fault, not the rig's)
      1 → job failed / timed_out / rig unreachable
      2 → setup error (bad artifact, etc.)
    """
    import json as _json
    import tempfile
    import time

    import requests
    from src.generation.ai_generator import (
        AIGenerationInputs,
        generate_ai_map,
    )
    from src.generation.gbx_writer import (
        GbxEmitError,
        emit_gbx_from_artifact,
    )

    config = load_config(args.config)
    gbx_cfg = (config.get("parsers") or {}).get("gbx") or {}
    executable = Path(
        gbx_cfg.get("executable")
        or "./parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper"
    )
    parser = SubprocessParser(
        executable=executable,
        parser_version=gbx_cfg.get("parser_version", "0.1.0"),
        timeout_seconds=float(gbx_cfg.get("timeout_seconds", 30.0)),
    )

    # Match the other remote-test commands: --token wins, then env.
    from src.remote_test.server import resolve_auth_token
    args.token = resolve_auth_token(args.token)

    conn = open_connection(config)
    try:
        inputs = AIGenerationInputs(
            base_map_id=args.base_map_id,
            random_seed=args.random_seed,
            beam_width=args.beam_width,
            max_interval_depth=args.max_interval_depth,
        )
        _LOG.info(
            "test-in-game step 1/5: generate-ai-map (base=%d seed=%d)",
            inputs.base_map_id, inputs.random_seed,
        )
        artifact = generate_ai_map(conn, inputs=inputs, config=config)

        _LOG.info(
            "test-in-game step 2/5: emit-gbx (run_id=%s)",
            artifact["run_id"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            emit_dir = Path(tmp) / "gbx"
            emit_dir.mkdir()
            try:
                gbx = emit_gbx_from_artifact(
                    conn, artifact=artifact,
                    parser=parser, output_dir=emit_dir,
                )
            except GbxEmitError as exc:
                _LOG.error("emit-gbx failed: %s", exc)
                return 2
            gbx_bytes = gbx.output_path.read_bytes()

            _LOG.info(
                "test-in-game step 3/5: enqueue to %s (size=%d)",
                args.server, len(gbx_bytes),
            )
            headers = (
                {"Authorization": f"Bearer {args.token}"}
                if args.token else {}
            )
            metadata = {
                "run_id": artifact["run_id"],
                "base_map_id": inputs.base_map_id,
                "random_seed": inputs.random_seed,
                "ai_generator_version": artifact["map"].get(
                    "ai_generator_version",
                ),
                "ai_confidence": (
                    artifact["finishability"].get("ai_confidence")
                ),
            }
            resp = requests.post(
                f"{args.server.rstrip('/')}/jobs",
                headers=headers,
                files={
                    "artifact": (
                        f"{artifact['run_id']}.Map.Gbx",
                        gbx_bytes, "application/octet-stream",
                    ),
                },
                data={
                    "run_id": artifact["run_id"],
                    "metadata": _json.dumps(metadata, sort_keys=True),
                    "timeout_seconds": str(args.timeout_seconds),
                },
                timeout=60,
            )
            if resp.status_code != 201:
                _LOG.error(
                    "enqueue failed: %d %s", resp.status_code, resp.text,
                )
                return 1
            job = resp.json()
            job_id = int(job["id"])
            _LOG.info(
                "test-in-game step 4/5: poll job %d (local timeout=%ds)",
                job_id, args.wait_seconds,
            )

    finally:
        conn.close()

    # --- poll outside the DB connection; the rig call doesn't need the
    # MariaDB conn and a long-held connection is wasteful ---
    deadline = time.monotonic() + float(args.wait_seconds)
    last_status = "queued"
    while time.monotonic() < deadline:
        r = requests.get(
            f"{args.server.rstrip('/')}/jobs/{job_id}",
            headers=headers, timeout=15,
        )
        if r.status_code != 200:
            _LOG.warning("poll got %d: %s", r.status_code, r.text)
            time.sleep(args.poll_interval)
            continue
        j = r.json()
        status = j["status"]
        if status != last_status:
            _LOG.info(
                "test-in-game status change: %s → %s (detail=%s)",
                last_status, status, j.get("detail") or "-",
            )
            last_status = status
        if status in ("complete", "failed", "timed_out", "cancelled"):
            _LOG.info("test-in-game step 5/5: done")
            _print_test_report(j)
            return 0 if status == "complete" else 1
        time.sleep(args.poll_interval)

    _LOG.error(
        "test-in-game timed out locally after %ds — job may still be "
        "running; query later with remote-test-status --job-id %d",
        args.wait_seconds, job_id,
    )
    return 1


def _print_test_report(job: dict[str, Any]) -> None:
    """Human-readable summary of a completed test job."""
    print("")
    print(f"  job_id         {job['id']}")
    print(f"  run_id         {job['run_id']}")
    print(f"  status         {job['status']}")
    print(f"  agent          {job.get('agent_id') or '-'}")
    print(f"  detail         {job.get('detail') or '-'}")
    report = job.get("report") or {}
    if not report:
        print("  (no telemetry report attached)")
        return
    print("")
    print("  telemetry:")
    print(f"    load_success      {report.get('load_success')}")
    if report.get("load_error"):
        print(f"    load_error        {report['load_error']}")
    print(f"    spawn_ok          {report.get('spawn_ok')}")
    print(f"    finished          {report.get('finished')}")
    # v0.2 plugin adds native-validation fields.
    vs = report.get("validation_status")
    if vs is not None:
        print(f"    validation_status {vs}")
    at = report.get("author_time_ms")
    if at is not None:
        print(f"    author_time_ms    {at}")
    print(f"    exit_reason       {report.get('exit_reason')}")
    print(f"    plugin_version    {report.get('plugin_version') or '-'}")
    cps = report.get("checkpoint_times_ms") or []
    if cps:
        print(f"    checkpoints       {len(cps)} times")
        for i, t in enumerate(cps):
            print(f"      [{i}] {t} ms")
    dc = report.get("driven_cells_count")
    if dc:
        print(f"    driven_cells      {dc} (head: {report.get('driven_cells_head') or []})")
    print("")


def _cmd_remote_test_agent(args: argparse.Namespace) -> int:
    """Run the Windows agent (PR2 of the remote-test rig).

    Works on any OS — ``windows-ness`` is a matter of where you
    point the tm_maps_root / plugin_rig_dir at. The loop is
    identical; only paths and the TM launch mechanism differ
    across hosts.
    """
    from src.remote_test_agent.agent import run_agent
    from src.remote_test_agent.config import load_config

    cfg = load_config(Path(args.config))
    max_iterations = (
        int(args.max_iterations) if args.max_iterations is not None else None
    )
    return run_agent(cfg, max_iterations=max_iterations)


def _cmd_remote_test_enqueue(args: argparse.Namespace) -> int:
    """Push a .Map.Gbx + metadata onto the queue."""
    import json as _json

    import requests
    artifact = Path(args.artifact)
    if not artifact.exists():
        _LOG.error("artifact not found: %s", artifact)
        return 2
    metadata: dict[str, Any] = {}
    if args.metadata:
        metadata = _json.loads(Path(args.metadata).read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            _LOG.error("metadata file must contain a JSON object")
            return 2
    if args.run_id is None and "run_id" in metadata:
        run_id = str(metadata["run_id"])
    else:
        run_id = args.run_id or artifact.stem
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    with artifact.open("rb") as fh:
        resp = requests.post(
            f"{args.server.rstrip('/')}/jobs",
            headers=headers,
            files={"artifact": (artifact.name, fh, "application/octet-stream")},
            data={
                "run_id": run_id,
                "metadata": _json.dumps(metadata, sort_keys=True),
                "timeout_seconds": str(args.timeout_seconds),
            },
            timeout=30,
        )
    if resp.status_code != 201:
        _LOG.error("enqueue failed: %d %s", resp.status_code, resp.text)
        return 1
    body = resp.json()
    _LOG.info(
        "enqueued job_id=%d run_id=%s sha256=%s size=%d",
        body["id"], body["run_id"],
        body["artifact_sha256"][:12] + "…",
        body["artifact_size"],
    )
    print(body["id"])
    return 0


def _cmd_remote_test_status(args: argparse.Namespace) -> int:
    """Query the queue for job(s). --job-id → one; --list N → recent."""
    import json as _json

    import requests
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    if args.job_id is not None:
        resp = requests.get(
            f"{args.server.rstrip('/')}/jobs/{int(args.job_id)}",
            headers=headers, timeout=15,
        )
        if resp.status_code == 404:
            _LOG.error("no such job: %d", args.job_id)
            return 2
        resp.raise_for_status()
        print(_json.dumps(resp.json(), indent=2))
        return 0
    resp = requests.get(
        f"{args.server.rstrip('/')}/jobs",
        headers=headers, params={"limit": int(args.list)}, timeout=15,
    )
    resp.raise_for_status()
    for j in resp.json().get("jobs", []):
        print(
            f"  {j['id']:>5}  {j['status']:<10}  run={j['run_id']:<18}  "
            f"agent={j['agent_id'] or '-':<16}  detail={j['detail'] or ''}"
        )
    return 0


def _cmd_validate_generation(args: argparse.Namespace) -> int:
    from src.generation.generator import validate_artifact_file
    config = load_config(args.config)
    conn = open_connection(config)
    try:
        summary = validate_artifact_file(
            conn,
            artifact_path=args.artifact,
            config=config,
            write_sidecar=not args.no_sidecar,
        )
    finally:
        conn.close()
    if summary is None:
        _LOG.info(
            "validate-generation: artifact %s has no assembled route "
            "(rejected at gate) — nothing to validate",
            args.artifact,
        )
        return 0
    _LOG.info(
        "validate-generation: %s fail=%d warn=%d info=%d codes=%s",
        args.artifact,
        summary.fail_count, summary.warn_count, summary.info_count,
        summary.code_counts or "-",
    )
    # Non-zero exit only on fail-severity findings; warns don't block.
    return 1 if summary.fail_count > 0 else 0


def _cmd_score_corridor_sequences(args: argparse.Namespace) -> int:
    from src.constraints.sequence_scoring import score_all_corridors
    config = load_config(args.config)
    conn = open_connection(config)
    try:
        counts = score_all_corridors(
            conn,
            map_id=int(args.map_id) if args.map_id is not None else None,
            limit=int(args.limit) if args.limit else None,
        )
    finally:
        conn.close()
    _LOG.info(
        "score-corridor-sequences: corridors=%d scored=%d null=%d",
        counts["corridors_seen"], counts["scored"], counts["null_scores"],
    )
    return 0


def _cmd_build_block_geometry(args: argparse.Namespace) -> int:
    from src.constraints.block_geometry import build_block_geometry
    config = load_config(args.config)
    conn = open_connection(config)
    try:
        families = args.family.split(",") if args.family else None
        report = build_block_geometry(conn, families=families)
    finally:
        conn.close()
    _LOG.info(
        "build-block-geometry: distinct=%d rows=%d",
        report.distinct_blocks_seen, report.rows_written,
    )
    for sc, n in sorted(report.shape_breakdown.items(), key=lambda p: -p[1]):
        _LOG.info("  %s: %d", sc, n)
    return 0


def _cmd_build_block_transition_counts(args: argparse.Namespace) -> int:
    from src.constraints.block_transitions import (
        build_block_transition_counts, reset_transition_counts,
    )
    config = load_config(args.config)
    conn = open_connection(config)
    try:
        if args.reset:
            reset_transition_counts(conn)
        report = build_block_transition_counts(
            conn,
            map_ids=[int(args.map_id)] if args.map_id is not None else None,
            limit=int(args.limit) if args.limit else None,
            include_triples=not args.no_triples,
        )
    finally:
        conn.close()
    _LOG.info(
        "build-block-transition-counts: maps=%d corridors=%d "
        "pair_transitions=%d pair_rows=%d "
        "triple_transitions=%d triple_rows=%d errors=%d",
        report.maps_seen, report.corridors_seen,
        report.transitions_counted, report.pairs_written,
        report.triple_transitions_counted, report.triples_written,
        len(report.errors),
    )
    return 0 if not report.errors else 1


def _cmd_compute_finishability_proof(args: argparse.Namespace) -> int:
    from src.generation.finishability_proof import compute_for_map
    config = load_config(args.config)
    gbx_cfg = (config.get("parsers") or {}).get("gbx") or {}
    executable = Path(
        args.parser_executable
        or gbx_cfg.get("executable")
        or "./parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper"
    )
    timeout = float(gbx_cfg.get("timeout_seconds", 30.0))
    parser_version = gbx_cfg.get("parser_version", "0.1.0")
    parser = SubprocessParser(
        executable=executable,
        parser_version=parser_version,
        timeout_seconds=timeout,
    )

    conn = open_connection(config)
    try:
        if args.map_id is not None:
            map_ids = [int(args.map_id)]
        else:
            # --all / default: every map with a raw_artifact_path.
            from src.storage.mariadb import cursor as cursor_ctx
            with cursor_ctx(conn) as cur:
                cur.execute(
                    "SELECT id FROM maps WHERE raw_artifact_path IS NOT NULL "
                    "AND parse_status = 'success' "
                    "ORDER BY id"
                    + (f" LIMIT {int(args.limit)}" if args.limit else "")
                )
                map_ids = [int(row[0]) for row in cur.fetchall()]

        ok = 0
        errors: list[tuple[int, str]] = []
        for mid in map_ids:
            try:
                compute_for_map(conn, mid, parser=parser)
                ok += 1
            except (FileNotFoundError, RuntimeError) as exc:
                errors.append((mid, str(exc)))
                _LOG.warning("compute-finishability-proof: map_id=%d failed: %s", mid, exc)
        _LOG.info(
            "compute-finishability-proof: maps_seen=%d ok=%d errors=%d",
            len(map_ids), ok, len(errors),
        )
        if errors:
            for mid, msg in errors[:5]:
                _LOG.warning("  fail map_id=%d: %s", mid, msg[:120])
    finally:
        conn.close()
    return 0 if not errors else 1


def _cmd_diagnose_strip(args: argparse.Namespace) -> int:
    import json as _json
    from src.generation.strip_diagnostics import (
        diagnose_strip, format_report_markdown,
    )
    from src.generation.gbx_writer import _lookup_base_gbx  # reuse helper
    from src.generation.finishability_proof import (  # noqa: F401 warm import
        fetch_proof,
    )
    config = load_config(args.config)
    gbx_cfg = (config.get("parsers") or {}).get("gbx") or {}
    executable = Path(
        args.parser_executable
        or gbx_cfg.get("executable")
        or "./parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper"
    )
    parser_version = gbx_cfg.get("parser_version", "0.1.0")
    parser = SubprocessParser(
        executable=executable,
        parser_version=parser_version,
        timeout_seconds=float(gbx_cfg.get("timeout_seconds", 30.0)),
    )

    # Load the generator artifact first — we need its base_map_id,
    # run_id, chosen-corridor cells, anchor cells, and the strip
    # policy it ran under for provenance.
    artifact = _json.loads(Path(args.artifact).read_text(encoding="utf-8"))
    base_map_id = int(artifact["inputs"]["base_map_id"])
    run_id = str(artifact.get("run_id") or "")
    strip_policy = str(
        (artifact.get("map") or {}).get("strip_policy") or "(none)"
    )

    # Chosen corridor IDs from the artifact's intervals. path_cells
    # don't live in the artifact (the IntervalEntry shape carries
    # corridor_id + path_length but not the cell list), so fetch from
    # route_corridors keyed on those ids.
    chosen_ids = [
        int(iv["chosen_corridor_id"])
        for iv in (artifact.get("route") or {}).get("intervals") or []
        if iv.get("chosen_corridor_id") is not None
    ]
    chosen_cells: list[tuple[int, int, int]] = []
    if chosen_ids:
        placeholders = ",".join(["%s"] * len(chosen_ids))
        conn = open_connection(config)
        try:
            from src.storage.mariadb import cursor as cursor_ctx
            with cursor_ctx(conn) as cur:
                cur.execute(
                    f"SELECT path_cells FROM route_corridors "
                    f"WHERE id IN ({placeholders})",
                    tuple(chosen_ids),
                )
                for (raw,) in cur.fetchall():
                    try:
                        data = _json.loads(raw) if raw else []
                    except (TypeError, _json.JSONDecodeError):
                        data = []
                    for c in data:
                        if isinstance(c, (list, tuple)) and len(c) == 3:
                            try:
                                chosen_cells.append(
                                    (int(c[0]), int(c[1]), int(c[2]))
                                )
                            except (TypeError, ValueError):
                                continue
        finally:
            conn.close()

    # Anchor cells from the artifact's map.checkpoints (grid-only) +
    # snapped free-placed anchors from map_checkpoints so the Spawn
    # neighbourhood shows up in the report.
    anchor_cells: list[tuple[str, int, tuple[int, int, int] | None]] = []
    for cp in (artifact.get("map") or {}).get("checkpoints") or []:
        tag = str(cp.get("tag") or "")
        order = int(cp.get("waypoint_order") or 0)
        x, y, z = cp.get("x"), cp.get("y"), cp.get("z")
        cell = (
            (int(x), int(y), int(z))
            if all(v is not None for v in (x, y, z))
            else None
        )
        anchor_cells.append((tag, order, cell))
    # Include free-placed anchors via the generator's snapping helper.
    from src.generation.generator import (
        _BLOCK_SIZE_X, _BLOCK_SIZE_Y, _BLOCK_SIZE_Z,
    )
    conn = open_connection(config)
    try:
        from src.storage.mariadb import cursor as cursor_ctx
        with cursor_ctx(conn) as cur:
            cur.execute(
                "SELECT tag, waypoint_order, abs_x, abs_y, abs_z "
                "FROM map_checkpoints "
                "WHERE map_id = %s AND placement = 'free' "
                "  AND abs_x IS NOT NULL",
                (base_map_id,),
            )
            for tag, order, ax, ay, az in cur.fetchall():
                snapped = (
                    int(float(ax) // _BLOCK_SIZE_X),
                    int(float(ay) // _BLOCK_SIZE_Y),
                    int(float(az) // _BLOCK_SIZE_Z),
                )
                anchor_cells.append((str(tag), int(order), snapped))
    finally:
        conn.close()

    # Fetch base GBX path + parse both GBX files via the wrapper.
    conn = open_connection(config)
    try:
        _title, base_path = _lookup_base_gbx(conn, base_map_id)
    finally:
        conn.close()
    stripped_path = Path(args.stripped_gbx)
    if not stripped_path.exists():
        _LOG.error("stripped GBX missing: %s", stripped_path)
        return 1

    base_result = parser.parse_map(base_path)
    if base_result.output is None:
        _LOG.error("failed parsing base: %s", base_result.error_detail)
        return 1
    stripped_result = parser.parse_map(stripped_path)
    if stripped_result.output is None:
        _LOG.error("failed parsing stripped: %s", stripped_result.error_detail)
        return 1

    report = diagnose_strip(
        base_map_id=base_map_id,
        base_map=base_result.output,
        stripped_map=stripped_result.output,
        chosen_corridor_cells=chosen_cells or None,
        anchor_cells=anchor_cells or None,
    )

    md = format_report_markdown(
        report, run_id=run_id, strip_policy=strip_policy,
    )
    out_dir = Path("reports") / "strip-diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"map{base_map_id}-{run_id or 'unknown'}.md"
    out_path.write_text(md, encoding="utf-8")
    _LOG.info(
        "diagnose-strip: wrote %s (%d drop(s), %d multicell candidates)",
        out_path,
        report.base_block_count - report.stripped_block_count,
        len(report.multicell_candidate_drops),
    )
    print(str(out_path))
    return 0


def _cmd_emit_gbx(args: argparse.Namespace) -> int:
    from src.generation.gbx_writer import (
        DEFAULT_GBX_OUTPUT_DIR,
        GbxEmitError,
        emit_gbx_from_artifact_file,
    )
    config = load_config(args.config)
    gbx_cfg = (config.get("parsers") or {}).get("gbx") or {}
    executable = Path(
        args.parser_executable
        or gbx_cfg.get("executable")
        or "./parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper"
    )
    timeout = float(gbx_cfg.get("timeout_seconds", 30.0))
    parser_version = gbx_cfg.get("parser_version", "0.1.0")
    parser = SubprocessParser(
        executable=executable,
        parser_version=parser_version,
        timeout_seconds=timeout,
    )
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_GBX_OUTPUT_DIR
    conn = open_connection(config)
    try:
        try:
            result = emit_gbx_from_artifact_file(
                conn,
                artifact_path=Path(args.artifact),
                parser=parser,
                output_dir=output_dir,
            )
        except GbxEmitError as exc:
            _LOG.error("emit-gbx failed: %s", exc)
            return 1
    finally:
        conn.close()
    _LOG.info(
        "emit-gbx: base_path=%s output_path=%s new_map_uid=%s "
        "block_count=%d baked_block_count=%d duration_ms=%d",
        result.base_path, result.output_path, result.new_map_uid,
        result.block_count, result.baked_block_count,
        result.subprocess_duration_ms,
    )
    # Also print the final path so shell chains can capture it.
    print(str(result.output_path))
    return 0


def _cmd_train_corridor_ranking(args: argparse.Namespace) -> int:
    from src.corridor.ranking import TrainingReport, train_and_evaluate
    config = load_config(args.config)
    sha = code_version()
    config_hash = resolve_config_hash(config)
    v2_outlier_sigma: float | None = (
        args.v2_outlier_sigma if args.v2_outlier_sigma > 0 else None
    )
    conn = open_connection(config)
    try:
        report = train_and_evaluate(
            conn,
            alpha=args.alpha,
            test_frac=args.test_frac,
            random_seed=args.random_seed,
            v2_aggregation_method=args.v2_aggregation,
            v2_trimmed_q=args.v2_trimmed_q,
            v2_outlier_sigma=v2_outlier_sigma,
            snapshot_id=args.snapshot,
        )
    finally:
        conn.close()

    def _log_scheme(r: TrainingReport) -> None:
        _LOG.info(
            "[%s] rows=%d train=%d test=%d alpha=%.3f seed=%d",
            r.label_scheme, r.total_rows, r.train_rows, r.test_rows,
            r.alpha, r.random_seed,
        )
        _LOG.info(
            "  train_rmse=%.4f test_rmse=%.4f "
            "test_rank_corr=%.4f heuristic_rank_corr=%.4f",
            r.train_rmse, r.test_rmse,
            r.test_rank_corr, r.heuristic_rank_corr,
        )
        _LOG.info(
            "  AUC learned=%s heuristic=%s delta=%s "
            "(n_maps_learned=%d n_maps_heuristic=%d)",
            f"{r.auc_learned:.4f}" if r.auc_learned is not None else "n/a",
            f"{r.auc_heuristic:.4f}" if r.auc_heuristic is not None else "n/a",
            f"{r.auc_delta:+.4f}" if r.auc_delta is not None else "n/a",
            r.n_maps_learned, r.n_maps_heuristic,
        )
        if args.verbose:
            for name, weight in zip(r.feature_names, r.weights):
                _LOG.info("  weight[%-30s] = %+.4f", name, weight)

    _LOG.info(
        "train-corridor-ranking: comparative run "
        "(maps with mean-interval-time=%d)",
        report.map_mean_interval_ms_count,
    )
    _log_scheme(report.inverse_rank)
    if report.time_envelope is not None:
        _log_scheme(report.time_envelope)
    if report.time_envelope_v2 is not None:
        _log_scheme(report.time_envelope_v2)
    if report.time_envelope_v2_weighted is not None:
        _log_scheme(report.time_envelope_v2_weighted)

    # Side-by-side deltas tracing the A1→A4 ladder, so the output at
    # a glance shows whether each refinement moved the signal.
    def _delta_line(
        baseline_name: str, scheme_name: str,
        baseline: TrainingReport | None, scheme: TrainingReport | None,
    ) -> None:
        if baseline is None or scheme is None:
            return
        _LOG.info(
            "delta (%s − %s): test_rank_corr=%+.4f  AUC=%s",
            scheme_name, baseline_name,
            scheme.test_rank_corr - baseline.test_rank_corr,
            (f"{(scheme.auc_learned - baseline.auc_learned):+.4f}"
             if (scheme.auc_learned is not None and baseline.auc_learned is not None)
             else "n/a"),
        )
    _delta_line(
        "inverse_rank", "time_envelope",
        report.inverse_rank, report.time_envelope,
    )
    _delta_line(
        "time_envelope", "time_envelope_v2",
        report.time_envelope, report.time_envelope_v2,
    )
    _delta_line(
        "time_envelope_v2", "time_envelope_v2_weighted",
        report.time_envelope_v2, report.time_envelope_v2_weighted,
    )

    if args.output:
        report.write_json(Path(args.output))
        _LOG.info("wrote comparative model report: %s", args.output)

    # Persist metrics history for the dashboard (PR B). Each scheme in
    # this training run becomes one row in model_metrics. Variety /
    # pred-stdev-ratio stay NULL here — the dashboard fills them live
    # from DB state when rendering, since those are properties of the
    # *deployed* model, not of a training pass.
    _persist_training_metrics(report, config, sha, config_hash)
    return 0


def _persist_training_metrics(
    report: "ComparativeTrainingReport",  # type: ignore[name-defined]
    config: dict,
    sha: str,
    config_hash: str,
) -> None:
    """Write one model_metrics row per scheme from the given training
    report. Best-effort — persistence failure logs a warning but does
    not break the training CLI."""
    from src.corridor.ranking.scoring_pipeline import compute_model_hash
    from src.corridor.ranking.model import RidgeRegression
    from src.learning import (
        MetricInsert,
        QualityInputs,
        ai_quality_score,
        new_run_id,
        record_many,
    )

    schemes: list[tuple[str, "TrainingReport"]] = []  # type: ignore[name-defined]
    if report.inverse_rank is not None:
        schemes.append(("inverse_rank", report.inverse_rank))
    if report.time_envelope is not None:
        schemes.append(("time_envelope", report.time_envelope))
    if report.time_envelope_v2 is not None:
        schemes.append(("time_envelope_v2", report.time_envelope_v2))
    if report.time_envelope_v2_weighted is not None:
        schemes.append((
            "time_envelope_v2_weighted", report.time_envelope_v2_weighted,
        ))
    if not schemes:
        return

    run_id = new_run_id()
    rows: list[MetricInsert] = []
    for scheme_name, sr in schemes:
        # Re-derive the model hash from the persisted weights so the
        # row can later be joined with route_corridors by hash.
        model = RidgeRegression(
            alpha=sr.alpha, feature_names=tuple(sr.feature_names),
        )
        import numpy as _np
        model.weights = _np.array(sr.weights, dtype=_np.float64)
        model_hash = compute_model_hash(model)

        quality = ai_quality_score(QualityInputs(
            test_rank_corr=sr.test_rank_corr,
            auc_delta=sr.auc_delta,
            # pred_stdev_ratio not available from TrainingReport — the
            # CLI doesn't hold onto the per-alpha sweep rows. Dashboard
            # computes this axis live from DB state.
        ))
        rows.append(MetricInsert(
            run_id=run_id,
            model_hash=model_hash,
            scheme=scheme_name,
            alpha=float(sr.alpha),
            n_labeled=int(sr.total_rows),
            code_version=sha,
            config_hash=config_hash,
            train_rmse=float(sr.train_rmse),
            test_rmse=float(sr.test_rmse),
            test_rank_corr=float(sr.test_rank_corr),
            heuristic_rank_corr=float(sr.heuristic_rank_corr),
            auc_learned=sr.auc_learned,
            auc_heuristic=sr.auc_heuristic,
            auc_delta=sr.auc_delta,
            ai_quality_score=quality,
            # pred_stdev, diversity, variety — not known at training
            # time; stay NULL, dashboard fills live.
        ))

    try:
        conn = open_connection(config)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("model_metrics persistence: db connect failed: %s", exc)
        return
    try:
        n = record_many(conn, rows)
        _LOG.info("persisted %d model_metrics rows (run_id=%s)", n, run_id)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("model_metrics persistence failed: %s", exc)
    finally:
        conn.close()


def _cmd_diagnose_corridor_ranking(args: argparse.Namespace) -> int:
    from src.corridor.ranking.diagnose import (
        DEFAULT_ALPHAS, run_diagnostics, write_report,
    )
    config = load_config(args.config)
    alphas: tuple[float, ...]
    if args.alphas:
        alphas = tuple(float(a) for a in args.alphas.split(","))
    else:
        alphas = DEFAULT_ALPHAS

    v2_outlier_sigma: float | None = (
        args.v2_outlier_sigma if args.v2_outlier_sigma > 0 else None
    )
    conn = open_connection(config)
    try:
        report = run_diagnostics(
            conn,
            alphas=alphas,
            production_alpha=args.production_alpha,
            v2_aggregation_method=args.v2_aggregation,
            v2_trimmed_q=args.v2_trimmed_q,
            v2_outlier_sigma=v2_outlier_sigma,
            snapshot_id=args.snapshot,
        )
    finally:
        conn.close()

    _LOG.info(
        "diagnose-corridor-ranking: corridors=%d maps_with_mean_interval=%d "
        "schemes=%s",
        report.total_corridors, report.maps_with_mean_interval,
        ",".join(s.label_scheme for s in report.schemes),
    )
    for scheme in report.schemes:
        if scheme.label_summary is not None:
            s = scheme.label_summary
            _LOG.info(
                "  [%s] label stdev=%.4f (N=%d, range=%.4f–%.4f)",
                scheme.label_scheme, s.stdev, s.count, s.minimum, s.maximum,
            )
        for row in scheme.sweep:
            _LOG.info(
                "  [%s] α=%g pred_stdev_all=%.4f test_rank_corr=%+.4f "
                "weight_l2=%.4f",
                scheme.label_scheme, row.alpha, row.pred_stdev_all,
                row.test_rank_corr, row.weight_l2_norm,
            )

    if args.output:
        write_report(report, Path(args.output))
        _LOG.info("wrote diagnostics report: %s", args.output)
    return 0


def _cmd_diagnose_corridor_diversity(args: argparse.Namespace) -> int:
    from src.diversity.metrics import build_report as build_diversity_report
    from src.diversity.metrics import fetch_paths
    from src.diversity.report import render_markdown as render_diversity_md
    config = load_config(args.config)
    conn = open_connection(config)
    try:
        paths = fetch_paths(conn, snapshot_id=args.snapshot)
    finally:
        conn.close()
    if not paths:
        _LOG.error("no corridors found — did you run build-route-corridors?")
        return 1
    report = build_diversity_report(paths, k=args.top_k)
    _LOG.info(
        "diagnose-corridor-diversity: corridors=%d maps=%d "
        "intervals_with_multiple=%d top_k=%d "
        "rank0_cross_map_median_jaccard=%.4f "
        "top_rank_virtual_fraction=%.4f "
        "top_rank_length_stdev=%.2f",
        report.total_corridors,
        report.corridor_owning_maps,
        len(report.intervals),
        report.top_k,
        report.rank0_cross_map_similarity_quartiles.get("median", 0.0),
        report.virtual_edge_fraction_top_rank,
        report.path_length_stdev_top_rank,
    )
    if report.heuristic_summary is not None and report.learned_summary is not None:
        h = report.heuristic_summary
        l = report.learned_summary
        _LOG.info(
            "  heuristic diversity median=%.4f | learned diversity median=%.4f "
            "(delta=%+.4f)",
            h.diversity_median, l.diversity_median,
            l.diversity_median - h.diversity_median,
        )
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_diversity_md(report), encoding="utf-8")
        _LOG.info("wrote diversity report: %s", args.output)
    return 0


def _cmd_compare_snapshots(args: argparse.Namespace) -> int:
    from src.learning.compare_snapshots import build_comparison, render_markdown
    config = load_config(args.config)
    conn = open_connection(config)
    try:
        comparison = build_comparison(
            conn,
            snapshot_a=args.snapshot_a,
            snapshot_b=args.snapshot_b,
            production_alpha=args.production_alpha,
        )
    finally:
        conn.close()
    _LOG.info(
        "compare-snapshots: A=%s (corridors=%d) B=%s (corridors=%d)",
        comparison.a.snapshot_id, comparison.a.total_corridors,
        comparison.b.snapshot_id, comparison.b.total_corridors,
    )
    # Surface the top-line diversity delta for at-a-glance scanning.
    a_d = comparison.a.diversity_delta_median
    b_d = comparison.b.diversity_delta_median
    if a_d is not None and b_d is not None:
        _LOG.info(
            "  diversity delta (learned − heuristic) median: A=%+.4f B=%+.4f (Δ=%+.4f)",
            a_d, b_d, b_d - a_d,
        )
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_markdown(comparison), encoding="utf-8")
        _LOG.info("wrote comparison report: %s", args.output)
    return 0


def _cmd_report_replay_coverage(args: argparse.Namespace) -> int:
    from src.coverage.replay_value import (
        CohortThresholdConfig,
        build_report,
        fetch_coverage,
    )
    from src.coverage.report import render_markdown
    config = load_config(args.config)
    thresholds = CohortThresholdConfig.from_config(config)
    conn = open_connection(config)
    try:
        maps = fetch_coverage(
            conn,
            thresholds=thresholds,
            snapshot_id=args.snapshot,
        )
    finally:
        conn.close()
    report = build_report(maps, top_n=args.top_n)
    _LOG.info(
        "report-replay-coverage: maps=%d corridor_owning=%d saturated=%d "
        "zero_replay_corridor=%d near_boundary=%d backfill_candidates=%d",
        report.total_maps,
        report.corridor_owning_maps,
        len(report.saturated_maps),
        len(report.zero_replay_corridor_maps),
        len(report.near_cohort_boundary_maps),
        len(report.backfill_recommendation),
    )
    if args.output:
        md = render_markdown(report)
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md, encoding="utf-8")
        _LOG.info("wrote coverage report: %s", args.output)
    return 0


def _cmd_score_corridors_learned(args: argparse.Namespace) -> int:
    from src.corridor.ranking.scoring_pipeline import (
        load_model_from_report,
        score_corridors_learned,
    )
    config = load_config(args.config)
    model, scheme_tag = load_model_from_report(Path(args.model_report))
    conn = open_connection(config)
    try:
        stats = score_corridors_learned(
            conn,
            model=model,
            learned_score_version=scheme_tag,
            map_ids=args.map_ids,
            snapshot_id=args.snapshot,
            limit=args.limit,
        )
    finally:
        conn.close()
    _LOG.info(
        "score-corridors-learned: maps_seen=%d updated=%d scored=%d "
        "scheme=%s model_hash=%s errors=%d",
        stats.maps_seen, stats.maps_updated, stats.corridors_scored,
        stats.learned_score_version, stats.model_hash[:12], len(stats.errors),
    )
    return 0 if not stats.errors else 1


def _cmd_score_route_corridors(args: argparse.Namespace) -> int:
    from src.corridor import score_corridors
    config = load_config(args.config)
    conn = open_connection(config)
    try:
        stats = score_corridors(
            conn, map_ids=args.map_ids,
            snapshot_id=args.snapshot, limit=args.limit,
        )
    finally:
        conn.close()
    _LOG.info(
        "score-route-corridors: maps_seen=%d updated=%d scored=%d "
        "score_version=%s errors=%d",
        stats.maps_seen, stats.maps_updated, stats.corridors_scored,
        stats.score_version, len(stats.errors),
    )
    return 0 if not stats.errors else 1


def _cmd_build_route_corridors(args: argparse.Namespace) -> int:
    from src.corridor.traversability import build_route_corridors
    config = load_config(args.config)
    conn = open_connection(config)
    try:
        stats = build_route_corridors(
            conn, map_ids=args.map_ids,
            snapshot_id=args.snapshot, limit=args.limit,
            top_n=args.top_n,
        )
    finally:
        conn.close()
    _LOG.info(
        "build-route-corridors: maps_seen=%d with_intervals=%d "
        "intervals=%d paths=%d top_n=%d errors=%d",
        stats.maps_seen, stats.maps_with_intervals,
        stats.intervals_written, stats.paths_written,
        stats.top_n, len(stats.errors),
    )
    return 0 if not stats.errors else 1


def _cmd_update_pattern_weights(args: argparse.Namespace) -> int:
    from src.corridor.traversability import update_pattern_weights
    config = load_config(args.config)
    conn = open_connection(config)
    try:
        stats = update_pattern_weights(conn)
    finally:
        conn.close()
    _LOG.info(
        "update-pattern-weights: pairs=%d edges_updated=%d max_count=%d errors=%d",
        stats.family_pairs_seen, stats.edges_updated,
        stats.max_pair_count, len(stats.errors),
    )
    return 0 if not stats.errors else 1


def _cmd_update_negative_evidence(args: argparse.Namespace) -> int:
    from src.corridor.traversability import update_negative_evidence
    config = load_config(args.config)
    conn = open_connection(config)
    try:
        stats = update_negative_evidence(
            conn, map_ids=args.map_ids,
            snapshot_id=args.snapshot, limit=args.limit,
            threshold=args.threshold,
        )
    finally:
        conn.close()
    _LOG.info(
        "update-negative-evidence: maps_seen=%d updated=%d examined=%d "
        "flagged=%d errors=%d",
        stats.maps_seen, stats.maps_updated, stats.edges_examined,
        stats.edges_flagged, len(stats.errors),
    )
    return 0 if not stats.errors else 1


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
        elif name == "route_corridor":
            stack.append(CorridorConfidenceEvaluator(conn))
        elif name == "route_corridor_learned":
            stack.append(CorridorLearnedEvaluator(conn))
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

    build_evidence_cmd = sub.add_parser(
        "build-traversability-evidence",
        help="Phase 3: materialize per-map traversability_edge_evidence rows "
             "(design note §6.1). Idempotent under classification_version.",
    )
    build_evidence_cmd.add_argument("--snapshot", type=str, default=None,
        help="restrict to maps in this ingestion_snapshot (default: all parsed)")
    build_evidence_cmd.add_argument(
        "--map-id", dest="map_ids", type=int, action="append", default=None,
        help="restrict to specific maps.id values (repeatable); overrides --snapshot",
    )
    build_evidence_cmd.add_argument("--limit", type=int, default=None,
        help="cap on map count for smoke runs")
    build_evidence_cmd.set_defaults(func=_cmd_build_traversability_evidence)

    update_path_support_cmd = sub.add_parser(
        "update-path-support",
        help="Phase 3 Signal 1: enumerate corridors per map, count edge "
             "usage across paths, UPDATE traversability_edge_evidence."
             "path_support_count.",
    )
    update_path_support_cmd.add_argument("--snapshot", type=str, default=None)
    update_path_support_cmd.add_argument(
        "--map-id", dest="map_ids", type=int, action="append", default=None,
        help="restrict to specific maps.id values (repeatable)",
    )
    update_path_support_cmd.add_argument("--limit", type=int, default=None)
    update_path_support_cmd.set_defaults(func=_cmd_update_path_support)

    update_negative_evidence_cmd = sub.add_parser(
        "update-negative-evidence",
        help="Phase 3 Signal 4: flag evidence edges in deco clusters. "
             "Sets negative_evidence_count to the count of NON_DRIVABLE "
             "axis-neighbors across both endpoint cells (max 12).",
    )
    update_negative_evidence_cmd.add_argument("--snapshot", type=str, default=None)
    update_negative_evidence_cmd.add_argument(
        "--map-id", dest="map_ids", type=int, action="append", default=None,
    )
    update_negative_evidence_cmd.add_argument("--limit", type=int, default=None)
    update_negative_evidence_cmd.add_argument(
        "--threshold", type=int, default=6,
        help="min combined NON_DRIVABLE neighbor count to flag (default 6 of 12)",
    )
    update_negative_evidence_cmd.set_defaults(func=_cmd_update_negative_evidence)

    update_pattern_weights_cmd = sub.add_parser(
        "update-pattern-weights",
        help="Phase 3 Signal 3: aggregate family-pair frequencies across "
             "the evidence table and write log-normalized pattern_weight "
             "to every row. Cross-map single-shot operation.",
    )
    update_pattern_weights_cmd.set_defaults(func=_cmd_update_pattern_weights)

    build_route_corridors_cmd = sub.add_parser(
        "build-route-corridors",
        help="Phase 3 D: persist enumerated corridor paths per (map, "
             "interval). Idempotent per classification_version.",
    )
    build_route_corridors_cmd.add_argument("--snapshot", type=str, default=None)
    build_route_corridors_cmd.add_argument(
        "--map-id", dest="map_ids", type=int, action="append", default=None,
    )
    build_route_corridors_cmd.add_argument("--limit", type=int, default=None)
    build_route_corridors_cmd.add_argument(
        "--top-n", type=int, default=100,
        help="keep the top N paths per interval (rank-ordered). 0 = keep all. "
             "Default 100.",
    )
    build_route_corridors_cmd.set_defaults(func=_cmd_build_route_corridors)

    score_route_corridors_cmd = sub.add_parser(
        "score-route-corridors",
        help="Combine the four evidence signals into a single "
             "corridor_confidence per route_corridors row.",
    )
    score_route_corridors_cmd.add_argument("--snapshot", type=str, default=None)
    score_route_corridors_cmd.add_argument(
        "--map-id", dest="map_ids", type=int, action="append", default=None,
    )
    score_route_corridors_cmd.add_argument("--limit", type=int, default=None)
    score_route_corridors_cmd.set_defaults(func=_cmd_score_route_corridors)

    generate_map_cmd = sub.add_parser(
        "generate-map",
        help="Phase 2 PR E — minimal generator. Copies a base map's "
             "blocks, runs route assembly + finishability gate, emits "
             "a schema-validated v0 JSON artifact.",
    )
    generate_map_cmd.add_argument(
        "--base-map-id", type=int, required=True,
        help="maps.id to use as the base (must be parsed)",
    )
    generate_map_cmd.add_argument(
        "--style-tag-filter", type=str, default=None,
        choices=("Tech", "FullSpeed"),
        help="v0 style filter (None for no filter)",
    )
    generate_map_cmd.add_argument(
        "--difficulty", type=str, default="medium",
        choices=("easy", "medium", "hard"),
    )
    generate_map_cmd.add_argument(
        "--random-seed", type=int, default=42,
    )
    generate_map_cmd.add_argument(
        "--output", type=str, default=None,
        help="write the artifact JSON to this path",
    )
    generate_map_cmd.add_argument(
        "--strip", action="store_true", default=False,
        help="Level-2: strip map.blocks to the chosen route + 1-cell "
             "grid-axis halo. Output is generation-v0.1.",
    )
    generate_map_cmd.set_defaults(func=_cmd_generate_map)

    ai_gen_cmd = sub.add_parser(
        "generate-ai-map",
        help="Phase 2 v0.2 — block-sequence AI generator. Synthesises "
             "a new route between a Linked-CP base map's anchors "
             "using pair-transition priors + block-geometry catalogue + "
             "diversity / validation signals. Emits a generation-v0.2 "
             "JSON artifact. Design: docs/generation/minimal-ai-generator-v0.md.",
    )
    ai_gen_cmd.add_argument(
        "--base-map-id", type=int, required=True,
        help="MariaDB maps.id of the Linked-CP base map (supplies anchors)",
    )
    ai_gen_cmd.add_argument(
        "--random-seed", type=int, default=42,
        help="deterministic seed — same (seed, map, corpus) → same run_id",
    )
    ai_gen_cmd.add_argument(
        "--style-tag-filter", type=str, default=None,
        choices=["Tech", "FullSpeed"],
        help="optional style filter (currently passed through for provenance)",
    )
    ai_gen_cmd.add_argument(
        "--difficulty", type=str, default="medium",
        choices=["easy", "medium", "hard"],
    )
    ai_gen_cmd.add_argument(
        "--beam-width", type=int, default=3,
        help="beam search width. 1 = greedy; wider explores alternate "
             "next-block picks per step and keeps the top-N globally. "
             "Default 3.",
    )
    ai_gen_cmd.add_argument(
        "--max-interval-depth", type=int, default=12,
        help="hard cap on synthesised blocks per interval",
    )
    ai_gen_cmd.add_argument(
        "--output", type=str, default=None,
        help="write the generation-v0.2 artifact JSON to this path",
    )
    ai_gen_cmd.set_defaults(func=_cmd_generate_ai_map)

    validate_gen_cmd = sub.add_parser(
        "validate-generation",
        help="Re-run geom + jump validators against a generation-v0 "
             "artifact JSON. Writes <artifact>.validation.json next "
             "to the input unless --no-sidecar. Exits non-zero when "
             "FAIL-severity findings are present; WARN-severity "
             "findings are informational.",
    )
    validate_gen_cmd.add_argument(
        "--artifact", type=str, required=True,
        help="path to a generation-v0 JSON artifact",
    )
    validate_gen_cmd.add_argument(
        "--no-sidecar", action="store_true", default=False,
        help="skip writing <artifact>.validation.json",
    )
    validate_gen_cmd.set_defaults(func=_cmd_validate_generation)

    # ---- Remote in-game test rig (Option A+ PR1/PR1 server) ----
    rt_serve = sub.add_parser(
        "remote-test-serve",
        help="Run the Linux-side queue/report server for the remote "
             "in-game test rig. Windows agents pull jobs + .Map.Gbx "
             "artifacts from this server and POST telemetry back.",
    )
    rt_serve.add_argument("--host", default="0.0.0.0")
    rt_serve.add_argument("--port", type=int, default=8787)
    rt_serve.add_argument(
        "--db", default="data/remote_test/jobs.db",
        help="SQLite database path (auto-created).",
    )
    rt_serve.add_argument(
        "--artifacts-root", default="data/remote_test/artifacts",
        help="Directory where uploaded .Map.Gbx files live.",
    )
    rt_serve.add_argument(
        "--token", default=None,
        help="Bearer token. Falls back to REMOTE_TEST_TOKEN env var.",
    )
    rt_serve.add_argument(
        "--allow-insecure", action="store_true", default=False,
        help="Disable bearer auth. Dev-only; do NOT use on a LAN.",
    )
    rt_serve.set_defaults(func=_cmd_remote_test_serve)

    rt_agent = sub.add_parser(
        "remote-test-agent",
        help="Run the Windows-side agent that pulls jobs from the "
             "Linux queue, downloads .Map.Gbx artifacts into TM2020's "
             "Maps/AI-inbox, signals the OpenPlanet plugin, and "
             "reports telemetry back.",
    )
    rt_agent.add_argument(
        "--config", required=True,
        help="Path to the agent YAML config. See "
             "src/remote_test_agent/README.md for schema.",
    )
    rt_agent.add_argument(
        "--max-iterations", type=int, default=None,
        help="Stop after this many poll cycles. Useful for smoke "
             "tests; omit to run until SIGINT/SIGTERM.",
    )
    rt_agent.set_defaults(func=_cmd_remote_test_agent)

    rt_enqueue = sub.add_parser(
        "remote-test-enqueue",
        help="Push a .Map.Gbx + metadata onto the queue via HTTP.",
    )
    rt_enqueue.add_argument(
        "--server", default="http://localhost:8787",
        help="Queue server URL.",
    )
    rt_enqueue.add_argument(
        "--token", default=None,
        help="Bearer token sent as Authorization header.",
    )
    rt_enqueue.add_argument(
        "--artifact", required=True,
        help="Path to the .Map.Gbx to enqueue.",
    )
    rt_enqueue.add_argument(
        "--run-id", default=None,
        help="Run id to tag this job with. Defaults to artifact stem "
             "or metadata.run_id.",
    )
    rt_enqueue.add_argument(
        "--metadata", default=None,
        help="Path to a JSON file with metadata (e.g. the generation "
             "artifact itself).",
    )
    rt_enqueue.add_argument(
        "--timeout-seconds", type=int, default=300,
    )
    rt_enqueue.set_defaults(func=_cmd_remote_test_enqueue)

    rt_status = sub.add_parser(
        "remote-test-status",
        help="Query the queue for a job (by --job-id) or list recent "
             "jobs (default).",
    )
    rt_status.add_argument(
        "--server", default="http://localhost:8787",
    )
    rt_status.add_argument("--token", default=None)
    rt_status.add_argument(
        "--job-id", type=int, default=None,
        help="Print full JSON for one job instead of the table.",
    )
    rt_status.add_argument(
        "--list", type=int, default=20,
        help="Number of recent jobs to list.",
    )
    rt_status.set_defaults(func=_cmd_remote_test_status)

    rt_e2e = sub.add_parser(
        "test-in-game",
        help="End-to-end loop: generate an AI map, emit its GBX, "
             "push it onto the remote-test queue, poll the Windows "
             "agent for telemetry, print the report. Closes the "
             "feedback loop for generator iteration.",
    )
    rt_e2e.add_argument("--base-map-id", type=int, required=True)
    rt_e2e.add_argument("--random-seed", type=int, default=42)
    rt_e2e.add_argument("--beam-width", type=int, default=3)
    rt_e2e.add_argument("--max-interval-depth", type=int, default=30)
    rt_e2e.add_argument(
        "--server", default="http://localhost:8787",
        help="Queue server URL.",
    )
    rt_e2e.add_argument(
        "--token", default=None,
        help="Bearer token. Falls back to REMOTE_TEST_TOKEN env var.",
    )
    rt_e2e.add_argument(
        "--timeout-seconds", type=int, default=300,
        help="Per-job timeout passed to the agent (sets when the "
             "agent gives up waiting on the plugin).",
    )
    rt_e2e.add_argument(
        "--wait-seconds", type=int, default=600,
        help="Local polling deadline. Separate from the agent-side "
             "timeout: this is how long test-in-game itself waits "
             "before giving up + returning non-zero.",
    )
    rt_e2e.add_argument(
        "--poll-interval", type=float, default=2.0,
    )
    rt_e2e.set_defaults(func=_cmd_test_in_game)

    emit_gbx_cmd = sub.add_parser(
        "emit-gbx",
        help="Phase 2 PR H — convert a generation-v0 JSON artifact to a "
             ".Map.Gbx file loadable in Trackmania. v0 copies the base "
             "map's original GBX and rewrites its identity fields; "
             "Level-2 block mutation ships separately.",
    )
    emit_gbx_cmd.add_argument(
        "--artifact", type=str, required=True,
        help="path to a generation-v0 JSON artifact",
    )
    emit_gbx_cmd.add_argument(
        "--output-dir", type=str, default=None,
        help="directory to write the .Map.Gbx into "
             "(default: reports/generated-gbx/)",
    )
    emit_gbx_cmd.add_argument(
        "--parser-executable", type=str, default=None,
        help="override path to the GBX wrapper binary "
             "(default from config.parsers.gbx.executable)",
    )
    emit_gbx_cmd.set_defaults(func=_cmd_emit_gbx)

    diag_strip_cmd = sub.add_parser(
        "diagnose-strip",
        help="#217 follow-up — compare base GBX vs a stripped emit "
             "and emit reports/strip-diagnostics/map<id>-<run_id>.md "
             "with per-anchor drops, multi-cell candidates, and a "
             "likely-reason hypothesis block. Evidence only; no fix.",
    )
    diag_strip_cmd.add_argument(
        "--artifact", type=str, required=True,
        help="path to the generator's generation-v0.1 JSON artifact",
    )
    diag_strip_cmd.add_argument(
        "--stripped-gbx", type=str, required=True,
        help="path to the stripped .Map.Gbx produced by emit-gbx",
    )
    diag_strip_cmd.add_argument(
        "--parser-executable", type=str, default=None,
    )
    diag_strip_cmd.set_defaults(func=_cmd_diagnose_strip)

    fin_proof_cmd = sub.add_parser(
        "compute-finishability-proof",
        help="Phase 2 PR M — compute + persist source-map "
             "finishability evidence (author times, WR from replays, "
             "proof_source) into map_finishability_proof. Generated-"
             "map safety gates are unaffected.",
    )
    fin_proof_cmd.add_argument(
        "--map-id", type=int, default=None,
        help="single map_id (default: scan every parsed map)",
    )
    fin_proof_cmd.add_argument(
        "--limit", type=int, default=None,
        help="cap the number of maps processed (for smoke runs)",
    )
    fin_proof_cmd.add_argument(
        "--parser-executable", type=str, default=None,
        help="override path to the GBX wrapper binary",
    )
    fin_proof_cmd.set_defaults(func=_cmd_compute_finishability_proof)

    transition_counts_cmd = sub.add_parser(
        "build-block-transition-counts",
        aliases=["build-block-pair-counts"],  # #218-1 compat
        help="Phase 2 #218-2 — extract ordered (A → B) pair and "
             "(A → B → C) triple block transition counts from "
             "route_corridors.path_cells into block_pair_transitions "
             "+ block_triple_transitions. Soft signal for generation "
             "weighting; never a hard constraint.",
    )
    transition_counts_cmd.add_argument(
        "--map-id", type=int, default=None,
        help="single map_id (default: scan every map with corridors)",
    )
    transition_counts_cmd.add_argument(
        "--limit", type=int, default=None,
        help="cap the number of maps processed (for smoke runs)",
    )
    transition_counts_cmd.add_argument(
        "--reset", action="store_true",
        help="TRUNCATE both transition tables before rebuilding",
    )
    transition_counts_cmd.add_argument(
        "--no-triples", action="store_true",
        help="skip triple extraction; populate pairs only",
    )
    transition_counts_cmd.set_defaults(func=_cmd_build_block_transition_counts)

    block_geometry_cmd = sub.add_parser(
        "build-block-geometry",
        help="Phase 2 #218-3 — classify every distinct (family, name) "
             "block into the block_geometry catalogue (shape class, "
             "surface hint, anchor-capable flag). Pattern-inferred; "
             "mesh-level accuracy is a future classifier_version bump.",
    )
    block_geometry_cmd.add_argument(
        "--family", type=str, default=None,
        help="comma-separated block_family filter (smoke runs)",
    )
    block_geometry_cmd.set_defaults(func=_cmd_build_block_geometry)

    seq_score_cmd = sub.add_parser(
        "score-corridor-sequences",
        help="Phase 2 #218-5 — compute combined_sequence_score "
             "(pattern × geometry) per route_corridors row. "
             "Assembly uses it as a tier-below tie-break after "
             "learned_corridor_score.",
    )
    seq_score_cmd.add_argument(
        "--map-id", type=int, default=None,
        help="single map_id (default: every corridor)",
    )
    seq_score_cmd.add_argument(
        "--limit", type=int, default=None,
        help="cap the number of corridors (for smoke runs)",
    )
    seq_score_cmd.set_defaults(func=_cmd_score_corridor_sequences)

    train_corridor_ranking_cmd = sub.add_parser(
        "train-corridor-ranking",
        help="Phase 4: train ridge-regression corridor ranking model "
             "and compare AUC vs corridor_confidence heuristic.",
    )
    train_corridor_ranking_cmd.add_argument("--alpha", type=float, default=1.0,
        help="ridge regularization strength (default 1.0)")
    train_corridor_ranking_cmd.add_argument("--test-frac", type=float, default=0.2,
        help="fraction of rows held out for test (default 0.2)")
    train_corridor_ranking_cmd.add_argument("--random-seed", type=int, default=42,
        help="random seed for the train/test split (default 42)")
    train_corridor_ranking_cmd.add_argument("--output", type=str, default=None,
        help="write the training report (JSON) to this path")
    train_corridor_ranking_cmd.add_argument("--verbose", action="store_true",
        help="log learned per-feature weights")
    train_corridor_ranking_cmd.add_argument(
        "--snapshot", type=str, default=None,
        help="restrict training to corridors from one ingestion snapshot "
             "(default: union across all snapshots)",
    )
    train_corridor_ranking_cmd.add_argument(
        "--v2-aggregation", type=str, default="trimmed_mean",
        choices=("mean", "median", "trimmed_mean"),
        help="v2 time-envelope aggregation method (default trimmed_mean)",
    )
    train_corridor_ranking_cmd.add_argument(
        "--v2-trimmed-q", type=float, default=0.1,
        help="trimmed_mean quantile for v2 (default 0.1)",
    )
    train_corridor_ranking_cmd.add_argument(
        "--v2-outlier-sigma", type=float, default=3.0,
        help="v2 outlier-rejection sigma; 0 or negative disables",
    )
    train_corridor_ranking_cmd.set_defaults(func=_cmd_train_corridor_ranking)

    diagnose_corridor_ranking_cmd = sub.add_parser(
        "diagnose-corridor-ranking",
        help="Phase 4 diagnostic: label-spread + regularization-sweep + "
             "feature-ablation to explain learned-score compression.",
    )
    diagnose_corridor_ranking_cmd.add_argument(
        "--alphas", type=str, default=None,
        help="comma-separated alphas for the regularization sweep "
             "(default 0.001,0.01,0.1,1.0,10.0,100.0)",
    )
    diagnose_corridor_ranking_cmd.add_argument(
        "--production-alpha", type=float, default=1.0,
        help="alpha for the feature-ablation pass (default 1.0 — matches "
             "current training default)",
    )
    diagnose_corridor_ranking_cmd.add_argument(
        "--output", type=str, default=None,
        help="write the diagnostics markdown report to this path",
    )
    diagnose_corridor_ranking_cmd.add_argument(
        "--v2-aggregation", type=str, default="trimmed_mean",
        choices=("mean", "median", "trimmed_mean"),
        help="time_envelope v2 aggregation method (default: trimmed_mean)",
    )
    diagnose_corridor_ranking_cmd.add_argument(
        "--v2-trimmed-q", type=float, default=0.1,
        help="trimmed_mean quantile for v2 (default 0.1 → drop top/bottom 10%%)",
    )
    diagnose_corridor_ranking_cmd.add_argument(
        "--v2-outlier-sigma", type=float, default=3.0,
        help="outlier rejection sigma for v2; pass 0 or negative to disable",
    )
    diagnose_corridor_ranking_cmd.add_argument(
        "--snapshot", type=str, default=None,
        help="restrict to one ingestion_snapshot so pre/post cohorts "
             "are comparable (e.g. 2026-04-scale-1k vs 2026-04-scale-3k-expansion)",
    )
    diagnose_corridor_ranking_cmd.set_defaults(
        func=_cmd_diagnose_corridor_ranking,
    )

    report_replay_coverage_cmd = sub.add_parser(
        "report-replay-coverage",
        help="A1: rank maps by expected value-to-learning from one "
             "more replay. Emits a backfill recommendation + buckets.",
    )
    report_replay_coverage_cmd.add_argument(
        "--snapshot", type=str, default=None,
        help="restrict to one ingestion_snapshot (e.g. 2026-04-scale-1k)",
    )
    report_replay_coverage_cmd.add_argument(
        "--top-n", type=int, default=200,
        help="number of backfill candidates to list (default 200)",
    )
    report_replay_coverage_cmd.add_argument(
        "--output", type=str, default=None,
        help="write the report (markdown) to this path",
    )
    report_replay_coverage_cmd.set_defaults(func=_cmd_report_replay_coverage)

    diagnose_corridor_diversity_cmd = sub.add_parser(
        "diagnose-corridor-diversity",
        help="A3: corridor diversity diagnostic — Jaccard-on-cells "
             "similarity, within-interval distribution, heuristic vs "
             "learned top-K collapse comparison.",
    )
    diagnose_corridor_diversity_cmd.add_argument(
        "--snapshot", type=str, default=None,
        help="restrict to one ingestion_snapshot (via parent map filter)",
    )
    diagnose_corridor_diversity_cmd.add_argument(
        "--top-k", type=int, default=3,
        help="top-K corridors per interval used for pairwise similarity (default 3)",
    )
    diagnose_corridor_diversity_cmd.add_argument(
        "--output", type=str, default=None,
        help="write the report (markdown) to this path",
    )
    diagnose_corridor_diversity_cmd.set_defaults(
        func=_cmd_diagnose_corridor_diversity,
    )

    compare_snapshots_cmd = sub.add_parser(
        "compare-snapshots",
        help="A/B comparison: run ranking + diversity diagnostics on "
             "two snapshots, emit one side-by-side markdown.",
    )
    compare_snapshots_cmd.add_argument(
        "--snapshot-a", type=str, required=True,
        help="ingestion_snapshot id for the A side (baseline)",
    )
    compare_snapshots_cmd.add_argument(
        "--snapshot-b", type=str, required=True,
        help="ingestion_snapshot id for the B side (new cohort)",
    )
    compare_snapshots_cmd.add_argument(
        "--production-alpha", type=float, default=1.0,
        help="alpha at which to report per-scheme metrics (default 1.0)",
    )
    compare_snapshots_cmd.add_argument(
        "--output", type=str, default=None,
        help="write the comparison report (markdown) to this path",
    )
    compare_snapshots_cmd.set_defaults(func=_cmd_compare_snapshots)

    score_corridors_learned_cmd = sub.add_parser(
        "score-corridors-learned",
        help="Phase 4: persist the learned corridor score to "
             "route_corridors.learned_corridor_score from a "
             "train-corridor-ranking model JSON.",
    )
    score_corridors_learned_cmd.add_argument(
        "--model-report", type=str, required=True,
        help="path to a train-corridor-ranking comparative report JSON",
    )
    score_corridors_learned_cmd.add_argument("--snapshot", type=str, default=None)
    score_corridors_learned_cmd.add_argument(
        "--map-id", dest="map_ids", type=int, action="append", default=None,
    )
    score_corridors_learned_cmd.add_argument("--limit", type=int, default=None)
    score_corridors_learned_cmd.set_defaults(func=_cmd_score_corridors_learned)

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
