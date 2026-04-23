"""Schema validation for the generated-map artifact.

Tiny wrapper over ``jsonschema`` that (1) loads the bundled
``generated_map.schema.json`` once, (2) exposes
:func:`validate_generated_map` returning either ``None`` (valid)
or a short human-readable error string, and (3) exposes the raw
schema dict for anyone who needs to build sub-structures.

Generator implementation PRs (PR E+) should call
:func:`validate_generated_map` before writing the artifact to disk;
failing validation yields the ``invalid_schema`` reject reason per
scope-v0.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

try:
    import jsonschema
except ImportError:                      # pragma: no cover
    jsonschema = None                    # type: ignore[assignment]


_SCHEMA_PATH = Path(__file__).with_name("generated_map.schema.json")


@functools.lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    """Load + cache the bundled JSON schema. LRU-cache keeps the
    file read off the hot path for generators that validate many
    artifacts per run."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_generated_map(payload: Any) -> str | None:
    """Return ``None`` if the payload conforms to the generated-map
    schema, or a short human-readable error string otherwise.

    Raises ``RuntimeError`` if the ``jsonschema`` package isn't
    installed; it's already a project dep via pyproject (used for
    benchmark manifest validation), so in practice this never fires.
    """
    if jsonschema is None:  # pragma: no cover
        raise RuntimeError(
            "jsonschema package not installed — "
            "add to dependencies before calling validate_generated_map"
        )
    schema = load_schema()
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        # Keep the error message one-line friendly — callers typically
        # feed it into the artifact's finishability.detail field.
        path = ".".join(str(p) for p in exc.absolute_path)
        return f"{path or '<root>'}: {exc.message}"
    return None
