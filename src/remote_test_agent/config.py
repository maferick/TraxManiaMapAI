"""Agent configuration loader.

YAML on disk, dataclass in memory. Every field has a sane default
so a brand-new config can be a single-liner with just
``server.url``. Env var ``REMOTE_TEST_TOKEN`` overrides config
file auth token so operators don't check secrets into their agent
folder.
"""
from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_LOG = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    url: str
    token: str | None = None
    verify_tls: bool = True

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ServerConfig":
        return ServerConfig(
            url=str(d.get("url", "")).rstrip("/") or "",
            token=d.get("token") or None,
            verify_tls=bool(d.get("verify_tls", True)),
        )


@dataclass
class AgentIdentity:
    id: str
    version: str = "agent-v0.1"
    hostname: str | None = None

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AgentIdentity":
        return AgentIdentity(
            id=str(d.get("id") or "").strip() or f"agent-{socket.gethostname()}",
            version=str(d.get("version") or "agent-v0.1"),
            hostname=(
                str(d.get("hostname")) if d.get("hostname")
                else socket.gethostname()
            ),
        )


@dataclass
class PathsConfig:
    tm_maps_root: Path          # Documents/Trackmania2020/Maps on Windows
    ai_inbox_subdir: str        # subfolder under tm_maps_root
    plugin_rig_dir: Path        # where the OpenPlanet plugin reads/writes

    @property
    def ai_inbox_dir(self) -> Path:
        return self.tm_maps_root / self.ai_inbox_subdir

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "PathsConfig":
        return PathsConfig(
            tm_maps_root=Path(str(d.get("tm_maps_root", "."))),
            ai_inbox_subdir=str(d.get("ai_inbox_subdir") or "AI-inbox"),
            plugin_rig_dir=Path(str(d.get("plugin_rig_dir", "."))),
        )


@dataclass
class PollingConfig:
    queue_interval_s: float = 3.0      # how often to call /jobs/next
    heartbeat_interval_s: float = 30.0 # how often to POST heartbeat
    plugin_poll_interval_s: float = 1.0 # how often to check for .out.json
    # Safety net — if the plugin doesn't respond within this window
    # (in addition to the job's own timeout_seconds), the agent bails
    # and reports failure. Keeps a crashed TM from eating jobs
    # forever.
    plugin_wait_max_extra_s: float = 60.0

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "PollingConfig":
        return PollingConfig(
            queue_interval_s=float(d.get("queue_interval_s", 3.0)),
            heartbeat_interval_s=float(d.get("heartbeat_interval_s", 30.0)),
            plugin_poll_interval_s=float(
                d.get("plugin_poll_interval_s", 1.0),
            ),
            plugin_wait_max_extra_s=float(
                d.get("plugin_wait_max_extra_s", 60.0),
            ),
        )


@dataclass
class AgentConfig:
    server: ServerConfig
    agent: AgentIdentity
    paths: PathsConfig
    polling: PollingConfig = field(default_factory=PollingConfig)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AgentConfig":
        return AgentConfig(
            server=ServerConfig.from_dict(d.get("server") or {}),
            agent=AgentIdentity.from_dict(d.get("agent") or {}),
            paths=PathsConfig.from_dict(d.get("paths") or {}),
            polling=PollingConfig.from_dict(d.get("polling") or {}),
        )


def load_config(path: Path) -> AgentConfig:
    """Read a YAML config + merge env overrides.

    ``REMOTE_TEST_TOKEN`` always wins over the file's
    ``server.token`` so operators can rotate creds without
    touching the repo copy.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"config at {path} must be a YAML object, got "
            f"{type(raw).__name__}"
        )
    cfg = AgentConfig.from_dict(raw)
    env_token = os.environ.get("REMOTE_TEST_TOKEN")
    if env_token:
        cfg.server.token = env_token.strip() or None
    if not cfg.server.url:
        raise ValueError("server.url is required in config")
    return cfg
