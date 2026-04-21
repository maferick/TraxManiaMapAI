"""Config loader + provenance helpers.

Loads ``config/settings.yaml``, layers values from a ``.env`` file
(if present), and substitutes ``${VAR}`` / ``${VAR:-default}``
tokens throughout the YAML. Also exposes helpers for canonical
config hashing and the current git SHA.

Env-var lookup order:

1. actually-set process environment (wins)
2. ``.env`` file at repo root (path overridable via ``TRAX_ENV_FILE``)

``.env`` never overrides a variable that's already exported. This
matches the convention every serious ``.env`` loader uses, and it
means CI/CD can export its own secrets and not be clobbered by a
committer's leftover local file.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

_LOG = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH_ENV = "TRAX_CONFIG"
_DEFAULT_CONFIG_RELATIVE = Path("config/settings.yaml")
_DEFAULT_ENV_FILE_ENV = "TRAX_ENV_FILE"
_DEFAULT_ENV_FILE = Path(".env")

# ${VAR} or ${VAR:-default}. Variable name is UPPER_SNAKE_CASE.
_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-(.*?))?\}")


class ConfigError(ValueError):
    """Raised for env-substitution failures and malformed config files."""


def _resolve_default_path() -> Path:
    env = os.environ.get(_DEFAULT_CONFIG_PATH_ENV)
    if env:
        return Path(env)
    return _DEFAULT_CONFIG_RELATIVE


def _resolve_env_file_path() -> Path:
    env = os.environ.get(_DEFAULT_ENV_FILE_ENV)
    if env:
        return Path(env)
    return _DEFAULT_ENV_FILE


def load_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse a ``.env`` file and merge its vars into ``os.environ``.

    Already-set environment variables are NOT overridden. Returns the
    dict of values that were *read* from the file (useful for tests);
    the merged environment is what subsequent lookups see.

    Syntax: ``KEY=VALUE`` per line; ``#`` line comments; optional
    ``export`` prefix; single or double quoted values have their
    quotes stripped. No variable interpolation inside the file — keep
    it simple; substitution happens in the YAML, not here.
    """
    target = path if path is not None else _resolve_env_file_path()
    if not target.is_file():
        return {}
    read_vars: dict[str, str] = {}
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.match(r"^[A-Z_][A-Z0-9_]*$", key):
            continue
        value = value.strip()
        # Strip matching surrounding quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        read_vars[key] = value
        os.environ.setdefault(key, value)
    return read_vars


def _substitute_token(match: re.Match[str], *, origin: str) -> str:
    var = match.group(1)
    default = match.group(2)
    val = os.environ.get(var)
    if val is not None:
        return val
    if default is not None:
        return default
    raise ConfigError(
        f"environment variable {var!r} is not set and has no default "
        f"(referenced from {origin})"
    )


def _substitute_value(value: Any, *, origin: str) -> Any:
    if isinstance(value, str):
        return _VAR_PATTERN.sub(lambda m: _substitute_token(m, origin=origin), value)
    if isinstance(value, dict):
        return {k: _substitute_value(v, origin=origin) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_value(v, origin=origin) for v in value]
    return value


def load_config(
    path: Path | None = None,
    *,
    env_file: Path | None = None,
    load_env: bool = True,
) -> dict[str, Any]:
    """Parse a YAML config and substitute ``${VAR}`` tokens.

    Parameters
    ----------
    path
        YAML file. Defaults to ``$TRAX_CONFIG`` or ``config/settings.yaml``.
    env_file
        Explicit ``.env`` path. Defaults to ``$TRAX_ENV_FILE`` or ``.env``
        at the current working directory.
    load_env
        If True (default), merge the ``.env`` file into ``os.environ``
        before substitution. Tests turn this off to isolate from any
        ambient ``.env``.
    """
    if load_env:
        load_env_file(env_file)

    resolved = path or _resolve_default_path()
    with resolved.open("r", encoding="utf-8") as fh:
        raw: object = yaml.safe_load(fh)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"config file {resolved} did not parse as a YAML mapping "
            f"(got {type(raw).__name__})"
        )
    return _substitute_value(raw, origin=str(resolved))


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
