"""Textual TUI — decision dashboard for the pipeline.

Earlier this was a status panel (counts + buttons). That surfaced
*what exists*; operators still had to interpret it. The decision
layer surfaces *what matters*:

- **Health** per subsystem (GREEN / YELLOW / RED)
- **Data coverage** fractions (the label-pool story in particular)
- **Bottlenecks** — flagged issues with a one-line fix suggestion
- **Freshness** — last-run age per pipeline stage

Raw counters stay alongside for when an operator wants the underlying
numbers; stage buttons stay unchanged (exclusive worker; log panel
streams subprocess output).

All four new panels come from a single DB snapshot (one transaction)
via :mod:`tools.dashboard.state`. The renderers are pure functions
in :mod:`tools.dashboard.render` — both modules are unit-tested.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
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


def _fetch_state_sync() -> "DashboardState":
    """One-shot DB snapshot. Lazy-imports project adapters so the
    dashboard boots even if the DB is unreachable — errors go into
    the DashboardState.error field rather than crashing the app."""
    from datetime import datetime, timezone
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from src.storage.mariadb import open_connection
        from src.utils.config import load_config
        from tools.dashboard.state import DashboardState, fetch_state
    except Exception as exc:  # noqa: BLE001
        from tools.dashboard.state import DashboardState
        return DashboardState(
            collected_at=datetime.now(tz=timezone.utc),
            error=f"import failed: {exc}",
        )
    try:
        conn = open_connection(load_config(None))
    except Exception as exc:  # noqa: BLE001
        return DashboardState(
            collected_at=datetime.now(tz=timezone.utc),
            error=f"db connect failed: {exc}",
        )
    try:
        return fetch_state(conn)
    finally:
        conn.close()


class Dashboard(App):
    """Textual app. See module docstring for layout."""

    CSS = """
    Screen { layout: vertical; }
    #top { height: 34; }
    #left-stack { width: 1fr; }
    #log { width: 2fr; border: round $secondary; padding: 1 2; }
    .panel { border: round $accent; padding: 0 1; margin: 0 1 0 0; }
    #health { height: 9; }
    #coverage { height: 12; }
    #learning { height: 9; }
    #diversity { height: 8; }
    #next_actions { height: 9; }
    #bottom-row { height: 13; }
    #a5-row { height: 9; }
    #bottlenecks { width: 2fr; }
    #freshness { width: 1fr; }
    #counters { width: 1fr; }
    #buttons { grid-size: 5 2; grid-gutter: 1 1; padding: 1 2; }
    Button { width: 100%; }
    #hint { dock: bottom; height: 1; padding: 0 2; color: $text-muted; }
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
            with Vertical(id="left-stack"):
                yield Static("(loading…)", id="health", classes="panel")
                yield Static("(loading…)", id="coverage", classes="panel")
                # A5 — learning + diversity + next-action row, decisions first.
                with Horizontal(id="a5-row"):
                    yield Static("(loading…)", id="learning", classes="panel")
                    yield Static("(loading…)", id="diversity", classes="panel")
                    yield Static("(loading…)", id="next_actions", classes="panel")
                with Horizontal(id="bottom-row"):
                    yield Static("(loading…)", id="bottlenecks", classes="panel")
                    with Vertical(id="right-stack"):
                        yield Static("(loading…)", id="freshness", classes="panel")
                        yield Static("(loading…)", id="counters", classes="panel")
            yield RichLog(id="log", highlight=True, markup=True, wrap=True)
        with Grid(id="buttons"):
            for i, stage in enumerate(STAGES):
                yield Button(stage.label, id=f"stage-{i}")
        yield Static("", id="hint")
        yield Footer()

    async def on_mount(self) -> None:
        self.action_refresh()
        # Periodic auto-refresh so external CLI runs (ingest jobs
        # started from another terminal, cron, etc.) become visible
        # without needing a manual 'r' keystroke.
        self.set_interval(10.0, self._refresh_state)

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
        """Fetch + render all panels in a worker so a slow DB doesn't
        block the UI thread. Workers are grouped so duplicate refreshes
        coalesce rather than queue, and won't collide with the
        stage-runner worker."""
        self.run_worker(
            self._refresh_state_async(),
            group="refresh_state",
            exclusive=True,
        )

    async def _refresh_state_async(self) -> None:
        from tools.dashboard.render import render_all
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(None, _fetch_state_sync)
        rendered = render_all(state)
        for panel_id, content in rendered.items():
            try:
                self.query_one(f"#{panel_id}", Static).update(content)
            except Exception:  # noqa: BLE001
                # Any missing widget is a CSS/layout drift — don't
                # crash the whole refresh for one panel.
                pass

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
        self._refresh_state()


def run() -> None:
    Dashboard().run()


if __name__ == "__main__":
    run()
