"""Phase 2: operator control layer — safe CLI action worker.

Turns existing `python -m src.cli ...` invocations into HTTP-triggered
actions with streaming stdout. The dashboard was read-only before this
module; with it, operators can drive the pipeline from the browser.

Design constraints (load-bearing):

- **Closed action catalogue.** The worker only accepts actions whose
  name appears in :data:`ACTIONS`. No arbitrary shell; no user-string
  passthrough to subprocess. Parameters are typed + validated per-action.
- **Single-action-at-a-time lock.** Phase-1 PR #31 showed the 4 GB host
  OOM-kills on parallel heavy stages. One action at a time, enforced by
  a module-level threading lock + state machine. Second action requests
  return HTTP 409 with the currently-running action name.
- **Subprocess isolation.** Actions shell out to `python -m src.cli` —
  the Flask process never imports CLI handlers. This keeps the dashboard
  process's memory budget independent of pipeline peak memory.
- **Bounded log buffer.** The running subprocess's stdout is tail-buffered
  (last N lines) so late-joining UI clients can catch up via polling.
  Memory cap ≈ N × avg-line-bytes; default 2000 lines ~ 200 KB per run.

Not in scope here:

- No persistent queue. If the Flask process restarts mid-run the
  action dies with it; operator re-triggers. Keep it simple.
- No auth. The dashboard is local-network tool; don't expose to hostile
  nets (see `tools/dashboard_web/app.py` threat-model note).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Tail buffer size — stdout lines retained for late-joining UI clients.
# 2000 lines × ~100 bytes = 200 KB per run; negligible on a 4 GB host.
_LOG_TAIL_SIZE = 2000


@dataclass(frozen=True)
class ActionSpec:
    """One row in the action catalogue. Maps a stable operator-facing
    name to the CLI invocation it expands to."""
    name: str                   # "ingest-maps-random" (URL-safe, stable)
    title: str                  # "Add maps (random N)" (for UI)
    hint: str                   # one-line description surfaced in UI
    # Returns the argv (without python/-m/src.cli). Receives the
    # validated params dict from :func:`validate_params`.
    cli_args: Callable[[dict[str, Any]], list[str]]
    # Validates + coerces the incoming params dict. Raises ValueError
    # with a human-readable message on bad input.
    validate_params: Callable[[dict[str, Any]], dict[str, Any]]
    # Rough expected duration, for UI progress hinting only (no timeout).
    expected_minutes: int


def _validate_random_count(params: dict[str, Any]) -> dict[str, Any]:
    raw = params.get("count", 200)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"count must be an integer, got {raw!r}")
    if n < 1 or n > 5000:
        raise ValueError(f"count must be in [1, 5000], got {n}")
    return {"count": n}


def _validate_snapshot_optional(params: dict[str, Any]) -> dict[str, Any]:
    snap = params.get("snapshot")
    if snap is None:
        return {}
    snap = str(snap).strip()
    if not snap:
        return {}
    # Snapshot ids are lowercase-alnum + hyphens by convention; reject
    # exotic input early. Not a strict security boundary (closed action
    # catalogue already limits arg shape), just a sanity guard.
    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,63}", snap):
        raise ValueError(
            f"snapshot id {snap!r} must match [a-z0-9-]{{2,64}}"
        )
    return {"snapshot": snap}


def _validate_empty(params: dict[str, Any]) -> dict[str, Any]:
    return {}


def _ingest_maps_random_args(p: dict[str, Any]) -> list[str]:
    return [
        "ingest-maps",
        "--random", str(p["count"]),
        "--snapshot", f"operator-{datetime.now(tz=timezone.utc).strftime('%Y%m%d-%H%M')}",
    ]


def _run_pipeline_args(p: dict[str, Any]) -> list[str]:
    # "Run Pipeline" composes the existing downstream CLIs into one
    # script-like invocation. Because the CLI doesn't have a
    # "run-pipeline" subcommand today, we fall back to a shell script
    # under scripts/ that chains them. The shell script lives on disk
    # so it's reviewable; we don't synth shell here.
    script = REPO_ROOT / "scripts" / "run_pipeline.sh"
    if not script.exists():
        raise ValueError(
            "scripts/run_pipeline.sh missing — can't run full pipeline"
        )
    return ["_shell", str(script)] + (
        ["--snapshot", p["snapshot"]] if "snapshot" in p else []
    )


def _train_ai_args(p: dict[str, Any]) -> list[str]:
    # Re-train on the current union and drop the resulting JSON where
    # score-corridors-learned can pick it up next.
    out = "reports/corridor-ranking-model-latest.json"
    return ["train-corridor-ranking", "--output", out, "--verbose"]


def _score_args(p: dict[str, Any]) -> list[str]:
    out = "reports/corridor-ranking-model-latest.json"
    return ["score-corridors-learned", "--model-report", out]


def _generate_stub_args(p: dict[str, Any]) -> list[str]:
    # Generator doesn't exist yet — PR C defines its scope before any
    # code lands. This stub action runs a sentinel command that exits
    # cleanly with a clear log line so operators see "not implemented
    # yet" rather than a broken button.
    return ["_noop", "generation stub — awaits PR C design doc"]


ACTIONS: dict[str, ActionSpec] = {
    "ingest-maps-random": ActionSpec(
        name="ingest-maps-random",
        title="Add maps (random)",
        hint="Pull N random maps from TMX and ingest them under a new snapshot.",
        cli_args=_ingest_maps_random_args,
        validate_params=_validate_random_count,
        expected_minutes=30,
    ),
    "run-pipeline": ActionSpec(
        name="run-pipeline",
        title="Run pipeline",
        hint="End-to-end: parse → build graph → classify → evidence → "
             "corridors → replays → clean → cohorts → score.",
        cli_args=_run_pipeline_args,
        validate_params=_validate_snapshot_optional,
        expected_minutes=90,
    ),
    "train-ai": ActionSpec(
        name="train-ai",
        title="Train AI",
        hint="Retrain the corridor-ranking model on current data (all four "
             "label schemes). Outputs a fresh JSON usable by `Score`.",
        cli_args=_train_ai_args,
        validate_params=_validate_empty,
        expected_minutes=2,
    ),
    "score-corridors": ActionSpec(
        name="score-corridors",
        title="Score corridors",
        hint="Apply the latest trained model to all corridors in the DB.",
        cli_args=_score_args,
        validate_params=_validate_empty,
        expected_minutes=1,
    ),
    "generate-map": ActionSpec(
        name="generate-map",
        title="Generate map",
        hint="(stub) generator lands after PR C's design doc.",
        cli_args=_generate_stub_args,
        validate_params=_validate_empty,
        expected_minutes=0,
    ),
}


# ---------------------------------------------------------------------
# Runtime state machine
# ---------------------------------------------------------------------

@dataclass
class ActionRun:
    """One in-flight or completed action run. Mutated from the worker
    thread; readers take a snapshot via :meth:`to_dict`."""
    id: str
    action_name: str
    title: str
    started_at: datetime
    cli_argv: list[str]
    completed_at: datetime | None = None
    exit_code: int | None = None
    status: str = "running"          # running | success | failed
    log_tail: deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_TAIL_SIZE))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def append_log(self, line: str) -> None:
        with self._lock:
            self.log_tail.append(line)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "action": self.action_name,
                "title": self.title,
                "started_at": self.started_at.isoformat(),
                "completed_at": (
                    self.completed_at.isoformat()
                    if self.completed_at is not None else None
                ),
                "status": self.status,
                "exit_code": self.exit_code,
                "cli_argv": list(self.cli_argv),
                "log_tail": list(self.log_tail),
            }


class ActionWorker:
    """Single-action-at-a-time runner. Use :meth:`start` to enqueue;
    second enqueue while busy raises :class:`BusyError`. The current
    run (if any) is reachable via :attr:`current`; the most recent
    completed run via :attr:`last_completed`."""

    class BusyError(Exception):
        """Raised when a new action is requested while one is running."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: ActionRun | None = None
        self._last_completed: ActionRun | None = None

    @property
    def current(self) -> ActionRun | None:
        with self._lock:
            return self._current

    @property
    def last_completed(self) -> ActionRun | None:
        with self._lock:
            return self._last_completed

    def start(
        self,
        action: ActionSpec,
        params: dict[str, Any],
        *,
        runner: Callable[[list[str], ActionRun], None] | None = None,
    ) -> ActionRun:
        """Validate + enqueue. Raises BusyError if an action is already
        running. ``runner`` is an override point for tests so they can
        stub out subprocess.Popen."""
        validated = action.validate_params(params)
        argv = action.cli_args(validated)
        run = ActionRun(
            id=uuid.uuid4().hex[:12],
            action_name=action.name,
            title=action.title,
            started_at=datetime.now(tz=timezone.utc),
            cli_argv=argv,
        )
        with self._lock:
            if self._current is not None and self._current.status == "running":
                raise ActionWorker.BusyError(
                    f"action {self._current.action_name!r} is running; "
                    "wait for it to finish"
                )
            self._current = run
        target = runner or self._default_runner
        thread = threading.Thread(
            target=target, args=(argv, run),
            name=f"action-{run.id}", daemon=True,
        )
        thread.start()
        return run

    def _default_runner(self, argv: list[str], run: ActionRun) -> None:
        """Subprocess runner. Special argv prefixes:
          - ``_noop`` → no-op action, logs the remaining argv as info
          - ``_shell`` → run argv[1:] as a shell command (used by pipeline script)
        Everything else is a python -m src.cli invocation."""
        try:
            if argv[:1] == ["_noop"]:
                run.append_log(f"[noop] {' '.join(argv[1:])}")
                self._finish(run, 0)
                return
            if argv[:1] == ["_shell"]:
                cmd = argv[1:]
            else:
                cmd = [sys.executable, "-m", "src.cli", *argv]
            run.append_log(f"$ {' '.join(cmd)}")
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ},
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                run.append_log(line.rstrip("\n"))
            rc = proc.wait()
            self._finish(run, rc)
        except Exception as exc:  # noqa: BLE001
            run.append_log(f"[worker error] {type(exc).__name__}: {exc}")
            self._finish(run, 1)

    def _finish(self, run: ActionRun, exit_code: int) -> None:
        with self._lock:
            run.completed_at = datetime.now(tz=timezone.utc)
            run.exit_code = exit_code
            run.status = "success" if exit_code == 0 else "failed"
            self._last_completed = run
            self._current = None


def tail_stream(
    run: ActionRun,
    *,
    start_offset: int = 0,
    poll_seconds: float = 0.3,
    idle_timeout_seconds: float = 30.0,
) -> Iterator[str]:
    """Yield log-tail lines starting at ``start_offset``. Blocks briefly
    for new lines while the action is running; stops at idle timeout or
    when the action completes and all lines are consumed.

    For Server-Sent Events, wrap each yielded line as ``data: ...\\n\\n``
    at the route layer — this generator keeps the SSE formatting out of
    the worker so non-SSE consumers (tests, future transports) can reuse
    the same stream."""
    idle_deadline = time.monotonic() + idle_timeout_seconds
    cursor = max(0, int(start_offset))
    while True:
        # Snapshot the deque under the run's lock so it doesn't mutate
        # mid-iteration. Cheap: the deque is bounded + this is a
        # polling loop, not a hot path.
        with run._lock:                                 # noqa: SLF001
            snapshot = list(run.log_tail)
            is_running = run.status == "running"
        if cursor < len(snapshot):
            for line in snapshot[cursor:]:
                yield line
            cursor = len(snapshot)
            idle_deadline = time.monotonic() + idle_timeout_seconds
            continue
        if not is_running:
            return
        if time.monotonic() > idle_deadline:
            return
        time.sleep(poll_seconds)
