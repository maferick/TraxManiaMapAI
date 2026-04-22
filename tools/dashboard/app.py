"""Textual TUI for running pipeline stages + viewing state.

Layout:
  ┌─ Header ──────────────────────────────────────────┐
  ├─ DB state (left)          ─ Log (right, scrolling)┤
  ├─ Stage buttons (grid)                             ┤
  └─ Footer (keys)                                    ┘

One stage runs at a time (exclusive worker). Buttons disable while a
stage is running; log streams from the subprocess stdout. Pressing
'r' refreshes the state panel; 'q' quits. Each stage is a
``python -m src.cli <subcommand>`` call — no direct library imports
so the tool can't accidentally drift from the CLI contract.

This is an OPS TUI, not a product. No auth, no data mutation beyond
what the CLI already does. Scope is read-mostly + pipeline-runner.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Header, RichLog, Static

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class Stage:
    """Single pipeline-stage invocation: label, CLI subcommand, and
    a human-readable one-liner for the bottom hint."""
    label: str
    cli_args: tuple[str, ...]
    hint: str


# Order mirrors the pipeline flow: migrations → ingest → parse → clean
# → cohort → graph → evaluate. Each stage shells out to
# `python -m src.cli <subcommand>`; args are passed after.
STAGES: tuple[Stage, ...] = (
    Stage("Migrate", ("migrate",), "apply pending MariaDB migrations"),
    Stage("Neo4j migrate", ("neo4j-migrate",), "apply pending Neo4j migrations"),
    Stage("Ingest maps (random 10)", ("ingest-maps", "--random", "10"),
          "TMX → maps table, 10 random maps"),
    Stage("Ingest replays (top-5×3)",
          ("ingest-replays", "--top-awards", "5", "--per-map", "3"),
          "3 replays each for the top-5 most-awarded maps"),
    Stage("Parse maps", ("parse-maps",), "run wrapper on unparsed maps, fill block_placements"),
    Stage("Parse replays", ("parse-replays",), "run wrapper on unparsed replays, emit sidecars"),
    Stage("Replay clean", ("replay-clean",), "telemetry OR breadcrumb rules → clean_status"),
    Stage("Assign cohorts", ("assign-cohorts",), "intent/performance/robustness labels"),
    Stage("Build constraint graph", ("build-graph",), "observed adjacencies → Neo4j"),
    Stage("Evaluator dry-run", ("eval-benchmark",), "score benchmark manifests, write report"),
)

STATE_QUERIES: tuple[tuple[str, str], ...] = (
    ("maps_total", "SELECT COUNT(*) FROM maps"),
    ("maps_parsed", "SELECT COUNT(*) FROM maps WHERE parse_status='success'"),
    ("replays_total", "SELECT COUNT(*) FROM replays"),
    ("replays_clean",
     "SELECT COUNT(*) FROM replays WHERE clean_status IN ('clean','usable_with_warnings')"),
    ("replays_with_breadcrumbs",
     "SELECT COUNT(*) FROM replays WHERE breadcrumbs_path IS NOT NULL"),
    ("replays_with_cohort",
     "SELECT COUNT(*) FROM replays WHERE cohort_membership IS NOT NULL"),
    ("waypoints", "SELECT COUNT(*) FROM map_checkpoints"),
    ("maps_with_waypoints",
     "SELECT COUNT(DISTINCT map_id) FROM map_checkpoints"),
    ("block_placements", "SELECT COUNT(*) FROM block_placements"),
    ("evaluation_results", "SELECT COUNT(*) FROM evaluation_artifacts"),
)


def _fetch_state() -> dict[str, int | str]:
    """One-shot DB state snapshot. Lazy-imports the project adapters so
    the dashboard boots even if the DB is unreachable — errors are
    surfaced into the counters rather than crashing the app."""
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from src.storage.mariadb import cursor, open_connection
        from src.utils.config import load_config
    except Exception as exc:  # noqa: BLE001
        return {"error": f"import failed: {exc}"}
    try:
        conn = open_connection(load_config(None))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"db connect failed: {exc}"}
    out: dict[str, int | str] = {}
    try:
        with cursor(conn) as cur:
            for name, sql in STATE_QUERIES:
                try:
                    cur.execute(sql)
                    row = cur.fetchone()
                    out[name] = int(row[0]) if row and row[0] is not None else 0
                except Exception as exc:  # noqa: BLE001
                    out[name] = f"error: {str(exc)[:40]}"
    finally:
        conn.close()
    return out


def _format_state(state: dict[str, int | str]) -> str:
    if "error" in state:
        return f"[red]{state['error']}[/red]"
    lines: list[str] = ["[b]Pipeline state[/b]"]
    mp = state.get("maps_parsed", 0)
    mt = state.get("maps_total", 0)
    lines.append(f"  maps:          [b]{mp}[/b] / {mt} parsed")
    bp = state.get("block_placements", 0)
    lines.append(f"  placements:    [b]{bp}[/b]")
    wp = state.get("waypoints", 0)
    wm = state.get("maps_with_waypoints", 0)
    lines.append(f"  waypoints:     [b]{wp}[/b] rows / {wm} maps")
    rt = state.get("replays_total", 0)
    rc = state.get("replays_clean", 0)
    rb = state.get("replays_with_breadcrumbs", 0)
    rco = state.get("replays_with_cohort", 0)
    lines.append(f"  replays:       [b]{rt}[/b] total")
    lines.append(f"    with breadcrumbs: {rb}")
    lines.append(f"    clean:            {rc}")
    lines.append(f"    with cohort:      {rco}")
    er = state.get("evaluation_results", 0)
    lines.append(f"  eval results:  [b]{er}[/b]")
    return "\n".join(str(line) for line in lines)


class Dashboard(App):
    """Textual app. See module docstring for layout."""

    CSS = """
    Screen { layout: vertical; }
    #top { height: 14; }
    #state { width: 1fr; border: round $accent; padding: 1 2; }
    #log { width: 2fr; border: round $secondary; padding: 1 2; }
    #buttons { grid-size: 5 2; grid-gutter: 1 1; padding: 1 2; }
    Button { width: 100%; }
    #hint { dock: bottom; height: 1; padding: 0 2; color: $text-muted; }
    .running { background: $warning 20%; }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh state"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._running = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top"):
            yield Static("(loading...)", id="state")
            yield RichLog(id="log", highlight=True, markup=True, wrap=True)
        with Grid(id="buttons"):
            for i, stage in enumerate(STAGES):
                yield Button(stage.label, id=f"stage-{i}")
        yield Static("", id="hint")
        yield Footer()

    async def on_mount(self) -> None:
        self.action_refresh()

    # ---- event handlers --------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._running:
            return
        button_id = event.button.id or ""
        if not button_id.startswith("stage-"):
            return
        idx = int(button_id.split("-", 1)[1])
        stage = STAGES[idx]
        self._run_stage(stage)

    def action_refresh(self) -> None:
        self._refresh_state()

    # ---- background work -------------------------------------------------

    def _refresh_state(self) -> None:
        """Fetch + render DB counters in a worker so a slow DB doesn't
        block the UI thread."""
        self.run_worker(self._refresh_state_async(), exclusive=False)

    async def _refresh_state_async(self) -> None:
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(None, _fetch_state)
        self.query_one("#state", Static).update(_format_state(state))

    def _run_stage(self, stage: Stage) -> None:
        self.run_worker(self._stream_stage(stage), exclusive=True)

    async def _stream_stage(self, stage: Stage) -> None:
        log = self.query_one("#log", RichLog)
        hint = self.query_one("#hint", Static)
        self._running = True
        hint.update(f"[yellow]running:[/yellow] {stage.label} — {stage.hint}")
        log.write(f"[bold cyan]▶ {stage.label}[/bold cyan]")
        log.write(f"  $ python -m src.cli {' '.join(stage.cli_args)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "src.cli", *stage.cli_args,
                cwd=str(REPO_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:  # noqa: BLE001
            log.write(f"[red]failed to spawn:[/red] {exc}")
            self._running = False
            hint.update("[red]spawn failed[/red]")
            return
        assert proc.stdout is not None
        async for line_bytes in proc.stdout:
            line = line_bytes.decode("utf-8", errors="replace").rstrip()
            log.write(line)
        rc = await proc.wait()
        if rc == 0:
            log.write(f"[green]✓ {stage.label} ({rc})[/green]")
            hint.update(f"[green]{stage.label}: done[/green]")
        else:
            log.write(f"[red]✗ {stage.label} (exit {rc})[/red]")
            hint.update(f"[red]{stage.label}: exit {rc}[/red]")
        self._running = False
        # Auto-refresh the state panel after any stage — most stages
        # change counts, and the user will want to see the update.
        self._refresh_state()


def run() -> None:
    Dashboard().run()


if __name__ == "__main__":
    run()
