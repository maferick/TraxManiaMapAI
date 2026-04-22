# Dashboard (ops TUI)

Terminal UI for operators. Read-mostly view of pipeline state plus one-click
invocations of each CLI stage. Not a product, not on the CLI-first critical
path — just a nicer on-ramp when running the pipeline interactively.

## Install

```bash
pip install -e ".[dashboard]"
```

Pulls in `textual` only. Not installed by default because core pipelines stay
CLI-first per `CLAUDE.md`.

## Launch

```bash
python -m tools.dashboard
```

## What it does

- Shows live DB counters: maps parsed, block placements, waypoints, replays
  by status (breadcrumbs / clean / cohort-assigned), evaluation results.
- One button per pipeline stage in canonical order (migrate → ingest → parse
  → clean → cohort → build-graph → eval-benchmark). Buttons shell out to
  `python -m src.cli <subcommand>` — same contract the CLI already enforces;
  the dashboard never imports pipeline code directly.
- Streams the subprocess log output into a scrolling pane.
- Refreshes state automatically after each stage completes. Press `r` to
  refresh manually.
- Press `q` to quit.

## Key behaviour

- One stage at a time. Buttons stay clickable during a run but the worker
  is marked `exclusive=True` so a second click is ignored until the current
  stage exits.
- Runs against the same `config/settings.yaml` the CLI uses — make sure the
  file exists before launching (copy `settings.example.yaml`, fill in
  secrets via `.env`).
- The DB query panel catches connection errors and surfaces them into the
  widget rather than crashing the TUI — useful for diagnosing dev setup
  without needing a terminal.

## What it is NOT

- Not a product UI. No auth, no user management, no data editing, no
  exposure on a network port.
- Not a migration path away from the CLI. Every action the TUI does is
  `python -m src.cli …` — the CLI remains the source of truth.
- Not a substitute for reading the dry-run report. The report is still
  Markdown at `reports/evaluator-dryrun-v1.md`; the TUI just triggers its
  regeneration.
