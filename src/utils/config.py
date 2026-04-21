"""Config loader + provenance helpers.

Scope for PR 3: read ``config/settings.yaml``, hash the resolved config
dict, resolve the current git SHA. Env-var overrides can be added when
a use case demands them — nothing in PR 3 requires them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

_LOG = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH_ENV = "TRAX_CONFIG"
_DEFAULT_CONFIG_RELATIVE = Path("config/settings.yaml")


def _resolve_default_path() -> Path:
    env = os.environ.get(_DEFAULT_CONFIG_PATH_ENV)
    if env:
        return Path(env)
    return _DEFAULT_CONFIG_RELATIVE


def load_config(path: Path | None = None) -> dict[str, Any]:
    resolved = path or _resolve_default_path()
    with resolved.open("r", encoding="utf-8") as fh:
        raw: object = yaml.safe_load(fh)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"config file {resolved} did not parse as a YAML mapping (got {type(raw).__name__})"
        )
    return raw


def resolve_config_hash(config: dict[str, Any]) -> str:
    """SHA-256 of the canonicalized config dict.

    Recorded on every ``stage_run`` and ``ingestion_snapshot`` row so an
    artifact can be reproduced from its upstream config. Canonicalization
    is JSON with sorted keys; non-JSON values are coerced via ``str``.
    """
    canonical = json.dumps(config, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def code_version() -> str:
    """Short git SHA of HEAD, or ``'unknown'`` if we can't resolve one."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5.0,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _LOG.debug("could not resolve git SHA: %s", exc)
        return "unknown"
    return result.stdout.strip() or "unknown"
