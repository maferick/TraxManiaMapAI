"""Capture per-frame ghost trajectories from TM2020, one replay per job.

This is the driver for the AIReplayTelemetry OpenPlanet plugin. It drops
an ``ai_rig_v1`` job descriptor into the plugin's storage folder, waits
for the plugin to play the replay's ghost and write its samples back,
then adapts the result into the canonical
:class:`src.replay.telemetry.ReplayTelemetry` artifact.

Why this exists: the offline GBX.NET path cannot decode TM2020 position
samples (``samples = 0``), so trajectories have to come from in-game
playback. See ``docs/workstreams/openplanet-telemetry.md``.

Requires TM2020 running with the AIReplayTelemetry plugin loaded. The
MCP server is not involved; this speaks the file-drop protocol directly,
same as ``tools/verify_map_in_game.py``.

Where the ghost comes from
--------------------------
By default no replay file is involved at all. The plugin calls
``Map_GetAuthorGhost`` on the loaded map, which hands back the author's
validation ghost that the map already carries internally. That is the
normal path for corpus work: nothing to extract, nothing to stage on
disk.

Pass ``--replay-file`` only to drive a ghost that is NOT the author's
(a leaderboard replay, or one of our own runs).

Measured on the corpus: 334 of the 545 Stadium2020 linked-checkpoint
maps embed an author ghost. The other 211 return ``no_author_ghost``,
which is an expected outcome rather than a failure.

Single job:

    python tools/capture_replay_telemetry.py \\
        --map-file "Maps/My Maps/whatever.Map.Gbx" \\
        --out-dir data/artifacts/telemetry

Batch (the gold-set pilot). Manifest is JSONL, one object per line with
``map_file`` and optionally ``id`` and ``replay_file``:

    python tools/capture_replay_telemetry.py \\
        --manifest data/gold_set_pilot.jsonl \\
        --out-dir data/artifacts/telemetry

Path note: ``map_file`` is consumed by ``PlayMap`` and wants a
game-resolvable path. ``replay_file``, when given, goes to
``Replay_Load``; try an absolute path first. If a job comes back with a
``Replay_Load failed`` error, retry that one with a path relative to the
user data folder (``Replays/Downloads/...``) before assuming the replay
is broken.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.replay.openplanet_adapter import (  # noqa: E402
    RIG_PROTOCOL,
    RigOutputError,
    telemetry_from_rig_output,
    telemetry_to_dict,
)
from src.replay.telemetry import from_dict as telemetry_from_dict  # noqa: E402

_LOG = logging.getLogger("capture-replay")

DEFAULT_STORAGE = (
    Path(os.environ.get("USERPROFILE", Path.home()))
    / "OpenplanetNext" / "PluginStorage" / "AIReplayTelemetry"
)
STORAGE = Path(os.environ.get("TM_REPLAY_STORAGE", DEFAULT_STORAGE))

# Generous by design. A job includes a map load, a replay load and a
# full playback, and the plugin enforces its own inner ceilings
# (PLAYBACK_WAIT_SECONDS = 300). This is only the outer backstop for a
# game that died or a plugin that never loaded.
DEFAULT_TIMEOUT_S = 420.0
POLL_S = 0.5


class RigTimeout(RuntimeError):
    pass


class RigNotRunning(RuntimeError):
    pass


def submit(
    map_file: str,
    replay_file: str = "",
    ghost_url: str = "",
    ghost_urls: list[str] | None = None,
    include_author: bool = False,
    *,
    run_id: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Run one job to completion and return the raw rig document."""
    if not STORAGE.is_dir():
        raise RigNotRunning(
            f"plugin storage not found: {STORAGE}\n"
            "Is TM2020 running with the AIReplayTelemetry plugin loaded?"
        )

    job_id = uuid.uuid4().int % 1_000_000_000
    stem = uuid.uuid4().hex[:12]
    in_path = STORAGE / f"{stem}.in.json"
    out_path = STORAGE / f"{stem}.out.json"

    # The plugin reads deadline_unix and gives up on its own once it
    # passes, so it must be shorter than our wait or we would sit here
    # after the plugin has already abandoned the job.
    deadline_unix = int(time.time() + timeout_s - 30)
    in_path.write_text(
        json.dumps(
            {
                "protocol": RIG_PROTOCOL,
                "job_id": job_id,
                "run_id": run_id,
                "map_file": map_file,
                "replay_file": replay_file,
                "ghost_url": ghost_url,
                "ghost_urls": ghost_urls or [],
                "include_author": include_author,
                "deadline_unix": deadline_unix,
            }
        ),
        encoding="utf-8",
    )
    _LOG.info("submitted job %s (map=%s)", stem, map_file)

    wall_deadline = time.monotonic() + timeout_s
    while time.monotonic() < wall_deadline:
        if out_path.is_file():
            try:
                doc = json.loads(out_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # Plugin may still be flushing; retry rather than fail.
                time.sleep(POLL_S)
                continue
            for path in (in_path, out_path):
                try:
                    path.unlink()
                except OSError:
                    pass
            return doc
        time.sleep(POLL_S)

    in_path.unlink(missing_ok=True)
    raise RigTimeout(
        f"no response for job {stem} within {timeout_s:.0f}s "
        "(is the plugin loaded and the game responsive?)"
    )


def _iter_manifest(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno}: bad JSON: {exc}") from exc
            if "map_file" not in entry:
                raise SystemExit(f"{path}:{lineno}: missing 'map_file'")
            yield entry


def _attempt(
    map_file: str,
    replay_file: str,
    ghost_url: str,
    ghost_urls: list[str] | None = None,
    include_author: bool = False,
    *,
    replay_id: str,
    run_id: str,
    timeout_s: float,
) -> tuple[str, dict[str, Any] | None, Any]:
    """One submit + adapt. Returns (status, raw_doc, telemetry_or_None)."""
    try:
        doc = submit(map_file, replay_file, ghost_url, ghost_urls,
                     include_author, run_id=run_id, timeout_s=timeout_s)
    except (RigTimeout, RigNotRunning) as exc:
        _LOG.error("%s: %s", replay_id, exc)
        return "failed", None, None

    if doc.get("exit_reason") == "no_author_ghost":
        return "skipped", doc, None

    try:
        telemetry = telemetry_from_rig_output(doc, source_replay_id=replay_id)
    except RigOutputError as exc:
        _LOG.warning(
            "%s: unusable (%s) exit_reason=%s",
            replay_id, exc, doc.get("exit_reason"),
        )
        return "failed", doc, None

    if not telemetry.extra.get("clock_rebased_to_race_start"):
        # The ghost never left the origin, so it never entered the
        # scene. Not a data property of the map: the same map succeeds
        # on a retry. Report it separately so the caller can retry
        # rather than discard a usable map.
        return "unanchored", doc, telemetry
    return "ok", doc, telemetry



def _split_multi(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """One rig document with ghosts_multi -> per-ghost single-shape docs.

    Each element is given the same envelope the single-ghost adapter
    reads, so multi-ghost capture needs no second adaptation path.
    """
    multi = doc.get("ghosts_multi") or []
    out = []
    for rec in multi:
        d = {
            "protocol": doc.get("protocol"),
            "job_id": doc.get("job_id"),
            "run_id": doc.get("run_id"),
            "load_success": doc.get("load_success"),
            "plugin_version": doc.get("plugin_version"),
            "sample_period_ms": doc.get("sample_period_ms"),
            "finished": rec.get("finished"),
            "exit_reason": rec.get("exit_reason"),
            "frame_count": len(rec.get("frames") or []),
            "ghost": rec.get("ghost") or {},
            "start_frame_index": rec.get("start_frame_index", -1),
            "frames": rec.get("frames") or [],
        }
        out.append(d)
    return out


def capture_one(
    map_file: str,
    replay_file: str = "",
    ghost_url: str = "",
    ghost_urls: list[str] | None = None,
    include_author: bool = False,
    *,
    replay_id: str,
    out_dir: Path,
    run_id: str,
    timeout_s: float,
    keep_raw: bool,
    retries: int = 1,
) -> str:
    """Run one job and write its telemetry artifact.

    Returns ``"ok"``, ``"skipped"`` or ``"failed"``. A map with no
    embedded author ghost is *skipped*, not failed: 211 of the 545
    gold-set maps are like that, and treating an expected corpus
    property as an error would make every batch report a red exit.

    A capture where the ghost never entered the scene is retried up to
    ``retries`` times before being given up on. That failure is
    transient (it hits the first job after a cold start), so discarding
    the map on one bad attempt would silently shrink the gold set.
    """
    status = "failed"
    doc = None
    telemetry = None
    for attempt in range(retries + 1):
        status, doc, telemetry = _attempt(
            map_file, replay_file, ghost_url, ghost_urls, include_author,
            replay_id=replay_id, run_id=run_id, timeout_s=timeout_s,
        )
        if status in ("ok", "skipped"):
            break
        if attempt < retries:
            _LOG.warning(
                "%s: attempt %d gave %s, retrying",
                replay_id, attempt + 1, status,
            )

    if status == "skipped":
        _LOG.info("%s: no embedded author ghost, skipping", replay_id)
        return "skipped"

    if keep_raw and doc is not None:
        raw_path = out_dir / f"{replay_id}.rig.json"
        raw_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        _LOG.debug("wrote raw rig output %s", raw_path)

    if telemetry is None:
        _LOG.error("%s: no usable capture after %d attempts",
                   replay_id, retries + 1)
        return "failed"

    multi = (doc or {}).get("ghosts_multi") or []
    if len(multi) > 1:
        wrote = 0
        for gi, gdoc in enumerate(_split_multi(doc), start=1):
            gid = f"{replay_id}#g{gi}"
            try:
                gtele = telemetry_from_rig_output(gdoc, source_replay_id=gid)
            except RigOutputError as exc:
                _LOG.warning("%s: unusable (%s)", gid, exc)
                continue
            if not gtele.extra.get("clock_rebased_to_race_start"):
                _LOG.warning("%s: no movement anchor, skipping", gid)
                continue
            gpayload = telemetry_to_dict(gtele)
            gpayload["extra"]["map_file"] = map_file
            telemetry_from_dict(gpayload)
            gpath = out_dir / f"{gid}.telemetry.json"
            gpath.write_text(json.dumps(gpayload, indent=2), encoding="utf-8")
            _LOG.info("%s: %d samples, finish=%s", gid,
                      len(gtele.samples), gtele.finish_time_ms)
            wrote += 1
        return "ok" if wrote else "failed"

    payload = telemetry_to_dict(telemetry)
    # Record which map file was driven. `maps` has no map_uid column, so
    # the join back to the corpus goes through the artifact content hash,
    # which is the basename corpus artifacts are stored under. Without
    # this the capture cannot be linked to its map at ingest time.
    payload["extra"]["map_file"] = map_file

    # Round-trip through the canonical parser before claiming success.
    # The workstream's acceptance bar is "passes from_dict validation",
    # so assert it here rather than discovering it at ingest time.
    telemetry_from_dict(payload)

    out_path = out_dir / f"{replay_id}.telemetry.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    _LOG.info(
        "%s: %d samples, %.1fs, finish=%s, checkpoints=%d -> %s",
        replay_id,
        len(telemetry.samples),
        telemetry.duration_ms / 1000.0,
        telemetry.finish_time_ms,
        len(telemetry.checkpoint_sample_indices),
        out_path,
    )
    if not telemetry.checkpoint_sample_indices:
        _LOG.warning(
            "%s: no checkpoint anchors, this replay cannot be used as "
            "gold-set ground truth", replay_id,
        )
        return "failed"
    return "ok"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--map-file")
    parser.add_argument("--replay-file")
    parser.add_argument("--id", help="stable id for the artifact filename")
    parser.add_argument("--manifest", type=Path,
                        help="JSONL of {map_file, replay_file, id?}")
    parser.add_argument("--out-dir", type=Path,
                        default=Path("data/artifacts/telemetry"))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--max-consecutive-failures", type=int, default=8,
        help="abort the batch after this many failures in a row; a wedged "
             "game fails every job and only a restart fixes it",
    )
    parser.add_argument("--keep-raw", action="store_true",
                        help="also write the unadapted rig .out.json")
    args = parser.parse_args()

    if bool(args.manifest) == bool(args.map_file):
        parser.error("give either --manifest or --map-file")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    _LOG.info("run %s, storage %s", run_id, STORAGE)

    if args.manifest:
        jobs = list(_iter_manifest(args.manifest))
    else:
        jobs = [{
            "map_file": args.map_file,
            "replay_file": args.replay_file or "",
            "id": args.id,
        }]

    # Circuit breaker. A wedged game reports "title script API never
    # ready" for every job, and without this the driver marches through
    # the whole manifest failing each one. That is not just wasted time:
    # it burns maps that a later resume would otherwise retry, and it
    # buries the one useful signal (the game needs restarting) under
    # thousands of identical errors. Measured twice: the game wedges
    # after an external interruption and never recovers on its own.
    tally = {"ok": 0, "skipped": 0, "failed": 0}
    consecutive_failures = 0
    for index, job in enumerate(jobs, start=1):
        if consecutive_failures >= args.max_consecutive_failures:
            _LOG.error(
                "ABORTING: %d consecutive failures. The game is almost "
                "certainly wedged (title script API never ready); restart "
                "TM2020 and re-run, resume skips what is already captured.",
                consecutive_failures,
            )
            break
        replay_id = (
            job.get("id")
            or Path(job.get("replay_file") or job["map_file"]).stem
        )
        _LOG.info("[%d/%d] %s", index, len(jobs), replay_id)
        status = capture_one(
            job["map_file"],
            job.get("replay_file") or "",
            job.get("ghost_url") or "",
            job.get("ghost_urls") or None,
            bool(job.get("include_author")),
            replay_id=replay_id,
            out_dir=args.out_dir,
            run_id=run_id,
            timeout_s=args.timeout,
            keep_raw=args.keep_raw,
        )
        tally[status] += 1
        consecutive_failures = 0 if status in ("ok", "skipped") else consecutive_failures + 1

    _LOG.info(
        "captured %d, skipped %d (no author ghost), failed %d, of %d",
        tally["ok"], tally["skipped"], tally["failed"], len(jobs),
    )
    return 1 if tally["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
