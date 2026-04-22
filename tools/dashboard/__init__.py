"""Operator TUI dashboard for the Trackmania AI pipeline.

Deliberately a separate tool from the ``src/`` package: it shells out
to ``python -m src.cli`` rather than importing pipeline code, so the
CLI-first contract in ``CLAUDE.md`` stays the single source of truth
for how stages are invoked. Nothing in ``src/`` imports from here.

Install dependency: ``pip install -e ".[dashboard]"`` (textual only).
Launch: ``python -m tools.dashboard``.
"""
