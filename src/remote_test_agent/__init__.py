"""Windows agent for the remote-test rig.

Designed to run on the operator's Windows gaming PC. Pulls jobs
from the Linux queue (outbound HTTPS/HTTP only — no inbound ports),
deploys the ``.Map.Gbx`` artifact into TM2020's ``Maps/AI-inbox``
folder, signals the OpenPlanet telemetry plugin via a file-drop
protocol, watches for the plugin's response, and ships results
back to the Linux server.

Deploys as a Python 3.10+ application. Dependencies: ``requests``
and ``pyyaml`` (already on the project's env).
"""
from src.remote_test_agent.agent import run_agent
from src.remote_test_agent.config import AgentConfig, load_config
from src.remote_test_agent.http_client import RemoteTestClient
from src.remote_test_agent.plugin_io import (
    PluginIO,
    TelemetryReport,
)

__all__ = [
    "AgentConfig",
    "PluginIO",
    "RemoteTestClient",
    "TelemetryReport",
    "load_config",
    "run_agent",
]
