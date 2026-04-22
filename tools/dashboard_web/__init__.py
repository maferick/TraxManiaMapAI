"""Flask web dashboard — same decision layer as the Textual TUI,
rendered as HTML so the dashboard survives terminal close and is
viewable from any device on the local network.

Entrypoint: ``python -m tools.dashboard_web`` → localhost:8765.
Read-only — pipeline stage-runs stay on the TUI / CLI.
"""
from tools.dashboard_web.app import create_app, run

__all__ = ["create_app", "run"]
