"""File-drop protocol between the Windows agent and the OpenPlanet
plugin.

Protocol v1
-----------

Directory: ``plugin_rig_dir`` (config-driven, e.g.
``OpenplanetNext\\PluginStorage\\ai_rig``).

Agent writes:    ``<job_id>.in.json``
Plugin writes:   ``<job_id>.out.json``
Both cleanup:    ``<job_id>.out.json`` is deleted by the agent
                 after it ingests the telemetry (acts as the
                 agent's ack); the plugin never removes files it
                 didn't author.

.in.json shape (what the agent writes):
  {
    "protocol": "ai_rig_v1",
    "job_id": <int>,
    "run_id": "<str>",
    "map_file": "<abs path to the .Map.Gbx in AI-inbox>",
    "deadline_unix": <int>,                     # monotonic deadline
    "metadata": { ... } | null                  # opaque, for logs
  }

.out.json shape (what the plugin writes):
  {
    "protocol": "ai_rig_v1",
    "job_id": <int>,
    "run_id": "<str>",
    "load_success": <bool>,
    "load_error": "<str>" | null,               # titlepack / missing
                                                #  resources / corrupt
    "spawn_ok": <bool>,                         # car spawned cleanly
    "finished": <bool>,                         # reached finish
    "checkpoint_times_ms": [<int>, ...],
    "driven_cells": [[x,y,z], ...],             # cells the car
                                                #  actually touched
    "exit_reason": "finished" | "quit" |
                   "respawn_limit" | "plugin_timeout" |
                   "load_error" | "other",
    "notes": "<str>" | null,
    "plugin_version": "<str>"
  }

Keep both documents intentionally small — large driven_cells
lists should be truncated / sampled plugin-side, not shipped
verbatim.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

PROTOCOL_VERSION: str = "ai_rig_v1"


@dataclass
class TelemetryReport:
    """Parsed form of the plugin's .out.json. Fields that the
    plugin didn't populate default to safe zeros so the agent
    can always ship a structurally-complete report upstream."""
    job_id: int
    run_id: str
    load_success: bool = False
    load_error: str | None = None
    spawn_ok: bool = False
    finished: bool = False
    checkpoint_times_ms: list[int] = field(default_factory=list)
    driven_cells: list[tuple[int, int, int]] = field(default_factory=list)
    exit_reason: str = "other"
    notes: str | None = None
    plugin_version: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_report_dict(self) -> dict[str, Any]:
        """Server-side ``POST /jobs/{id}/status`` ``report`` field."""
        return {
            "load_success": self.load_success,
            "load_error": self.load_error,
            "spawn_ok": self.spawn_ok,
            "finished": self.finished,
            "checkpoint_times_ms": list(self.checkpoint_times_ms),
            "driven_cells_count": len(self.driven_cells),
            # Full driven_cells are bulky — ship a summary to the
            # server; leave the full list to sit in the plugin
            # storage folder for offline analysis.
            "driven_cells_head": [list(c) for c in self.driven_cells[:20]],
            "exit_reason": self.exit_reason,
            "notes": self.notes,
            "plugin_version": self.plugin_version,
        }


class PluginIOError(Exception):
    """Malformed / missing plugin output. Agent fails the job."""


class PluginIO:
    """Stateless helper for writing .in.json and reading .out.json."""

    def __init__(self, rig_dir: Path) -> None:
        self._dir = Path(rig_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def drop_trigger(
        self,
        *,
        job_id: int,
        run_id: str,
        map_file: Path,
        deadline_unix: int,
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        """Write ``<job_id>.in.json`` atomically (tmp + rename) so
        the plugin never sees a half-written file if the agent
        dies mid-write."""
        target = self._dir / f"{int(job_id)}.in.json"
        payload = {
            "protocol": PROTOCOL_VERSION,
            "job_id": int(job_id),
            "run_id": run_id,
            "map_file": str(map_file),
            "deadline_unix": int(deadline_unix),
            "metadata": metadata or None,
        }
        tmp = target.with_suffix(".in.json.tmp")
        tmp.write_text(
            json.dumps(payload, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, target)
        return target

    def clear_stale(self, job_id: int) -> None:
        """Best-effort cleanup from a prior run — removes .in.json
        + .out.json for the given id. Called before drop_trigger so
        the agent doesn't read a previous job's .out.json."""
        for suffix in (".in.json", ".out.json"):
            p = self._dir / f"{int(job_id)}{suffix}"
            if p.exists():
                try:
                    p.unlink()
                except OSError as exc:
                    _LOG.warning("could not remove %s: %s", p, exc)

    def wait_for_report(
        self,
        *,
        job_id: int,
        deadline_unix: int,
        poll_interval_s: float = 1.0,
    ) -> TelemetryReport | None:
        """Busy-poll for ``<job_id>.out.json`` until ``deadline_unix``.

        Returns ``None`` on timeout — the caller decides whether to
        report ``timed_out`` or ``failed`` upstream.
        """
        target = self._dir / f"{int(job_id)}.out.json"
        while time.time() < deadline_unix:
            if target.exists():
                try:
                    return _parse_report(target)
                except PluginIOError as exc:
                    _LOG.error(
                        "plugin report at %s was malformed: %s",
                        target, exc,
                    )
                    # Remove so we don't re-read the bad file.
                    try:
                        target.unlink()
                    except OSError:
                        pass
                    return None
            time.sleep(max(0.1, poll_interval_s))
        return None

    def ack(self, job_id: int) -> None:
        """Agent's ack: remove the .out.json after ingest."""
        p = self._dir / f"{int(job_id)}.out.json"
        if p.exists():
            try:
                p.unlink()
            except OSError as exc:
                _LOG.warning("ack cleanup failed for %s: %s", p, exc)


def _parse_report(path: Path) -> TelemetryReport:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginIOError(f"read/decode failed: {exc}") from exc
    if not isinstance(body, dict):
        raise PluginIOError("root must be a JSON object")
    if body.get("protocol") != PROTOCOL_VERSION:
        raise PluginIOError(
            f"protocol mismatch: expected {PROTOCOL_VERSION!r}, "
            f"got {body.get('protocol')!r}"
        )
    if "job_id" not in body:
        raise PluginIOError("job_id required")
    cells_raw = body.get("driven_cells") or []
    cells: list[tuple[int, int, int]] = []
    if isinstance(cells_raw, list):
        for c in cells_raw:
            if isinstance(c, (list, tuple)) and len(c) == 3:
                try:
                    cells.append((int(c[0]), int(c[1]), int(c[2])))
                except (TypeError, ValueError):
                    continue
    return TelemetryReport(
        job_id=int(body["job_id"]),
        run_id=str(body.get("run_id") or ""),
        load_success=bool(body.get("load_success") or False),
        load_error=(
            str(body["load_error"]) if body.get("load_error") else None
        ),
        spawn_ok=bool(body.get("spawn_ok") or False),
        finished=bool(body.get("finished") or False),
        checkpoint_times_ms=[
            int(t) for t in (body.get("checkpoint_times_ms") or [])
            if isinstance(t, (int, float))
        ],
        driven_cells=cells,
        exit_reason=str(body.get("exit_reason") or "other"),
        notes=str(body["notes"]) if body.get("notes") else None,
        plugin_version=(
            str(body["plugin_version"]) if body.get("plugin_version") else None
        ),
        raw=body,
    )
