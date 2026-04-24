"""Phase 2 PR H — JSON artifact → .Map.Gbx writer.

Orchestrates the subprocess call that produces a Trackmania-loadable
map file from one of our generation-v0 JSON artifacts. Copy-from-base
for v0: load the base map's original ``.Map.Gbx``, rewrite its
``MapUid`` + ``MapName`` to make it a distinct in-game entity, save.
Level-1 mutation doesn't change block geometry, so the emitted map is
the base map with a new identity — useful for round-trip validation
and for telling seed-42 from seed-1000 apart in the operator's
local Trackmania folder.

Block-level mutation (Level 2: strip-to-route) extends the C# side
(MapEmitter.cs) with block filtering; this module's public API stays
the same.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pymysql.connections import Connection

from src.parsers import SubprocessParser
from src.parsers.errors import ParseStatus
from src.storage.mariadb import cursor

_LOG = logging.getLogger(__name__)

# v0 default for emitted GBX artifacts. Per-run filenames embed
# base_map_id + run_id so directory listings stay human-scannable.
DEFAULT_GBX_OUTPUT_DIR: Path = Path("reports") / "generated-gbx"

# TM2020 map UIDs are 27 characters, base64-url-safe (no padding).
# We derive ours deterministically from the artifact's run_id so
# re-emitting the same artifact produces the same in-game identity
# — matches scope-v0 §Provenance "same inputs → same run_id" across
# the whole chain, GBX included.
_MAP_UID_LEN: int = 27
_URL_SAFE_BASE64_RE = re.compile(r"[A-Za-z0-9_-]")


class GbxEmitError(RuntimeError):
    """Raised when the wrapper subprocess fails to emit a valid GBX.

    Translated to CLI exit=1 by callers; operator sees the detail on
    the dashboard's action-log stream.
    """


@dataclass(frozen=True)
class GbxEmitResult:
    """What :func:`emit_gbx_from_artifact` returns on success."""
    output_path: Path
    new_map_uid: str
    new_map_name: str
    base_path: Path
    block_count: int
    baked_block_count: int
    subprocess_duration_ms: int
    # Level-2 strip diagnostic. 0 on non-stripped emits.
    source_block_count: int = 0
    removed_block_count: int = 0
    stripped: bool = False


# ---------------------------------------------------------------------
# UID / name derivation
# ---------------------------------------------------------------------

def _derive_map_uid(run_id: str) -> str:
    """Produce a 27-char base64-url-safe UID deterministically from
    ``run_id``. Same run_id → same UID, so re-emitting the artifact
    doesn't create a spurious new in-game map identity.

    Uses blake2b over the run_id then base64-url-safe encodes the
    first 20 bytes → 27 chars (no padding). The output alphabet is
    [A-Za-z0-9_-], which TM2020 accepts as a UID.
    """
    import base64
    digest = hashlib.blake2b(run_id.encode("utf-8"), digest_size=20).digest()
    b64 = base64.urlsafe_b64encode(digest).rstrip(b"=")
    uid = b64.decode("ascii")[:_MAP_UID_LEN]
    if len(uid) < _MAP_UID_LEN:
        # 20 bytes → ceil(20*8/6) = 27 chars, so we never under-run;
        # left as a defensive guard in case blake2b's output shape
        # changes in a future stdlib.
        uid = uid.ljust(_MAP_UID_LEN, "A")
    return uid


def _derive_map_name(
    *, base_title: str | None, run_id: str, seed: int, verified: bool,
) -> str:
    """Human-readable title the operator sees in-game. Carries enough
    provenance (base title, seed, verify state) to identify the map
    in a list without opening the file."""
    base = base_title or "generated"
    marker = "verified" if verified else "rejected"
    short_run = run_id[:8]
    return f"{base} · gen #{short_run} · seed {seed} · {marker}"


# ---------------------------------------------------------------------
# DB lookup
# ---------------------------------------------------------------------

_RAW_ARTIFACT_SQL = """
SELECT title, raw_artifact_path
FROM maps
WHERE id = %s
"""


def _lookup_base_gbx(conn: Connection, base_map_id: int) -> tuple[str | None, Path]:
    """Fetch the base map's title + on-disk ``.Map.Gbx`` path from the
    DB. Raises :class:`GbxEmitError` if the map or its artifact are
    missing — both are operator-fixable states (broken ingest, manual
    artifact deletion), not generator bugs."""
    with cursor(conn) as cur:
        cur.execute(_RAW_ARTIFACT_SQL, (base_map_id,))
        row = cur.fetchone()
    if row is None:
        raise GbxEmitError(
            f"base map_id={base_map_id} not found in maps table"
        )
    title, raw_path = row
    if not raw_path:
        raise GbxEmitError(
            f"map_id={base_map_id} has no raw_artifact_path on record; "
            "the base GBX was never persisted (re-ingest to recover)"
        )
    path = Path(str(raw_path))
    if not path.is_absolute():
        # Legacy rows may store repo-relative paths; resolve against
        # REPO_ROOT (the generator process's cwd is the repo root per
        # dashboard_web / scripts convention).
        path = Path.cwd() / path
    if not path.exists():
        raise GbxEmitError(
            f"base map_id={base_map_id} raw_artifact_path missing on disk: {path}"
        )
    return (str(title) if title is not None else None), path


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def emit_gbx_from_artifact(
    conn: Connection,
    *,
    artifact: dict[str, Any],
    parser: SubprocessParser,
    output_dir: Path | None = None,
) -> GbxEmitResult:
    """Emit one ``.Map.Gbx`` file from a generation-v0 artifact dict.

    Parameters
    ----------
    conn : MariaDB connection, used to resolve the base map's original
        GBX artifact path from ``maps.raw_artifact_path``.
    artifact : a fully-validated generation-v0 dict (what
        :func:`src.generation.generate_from_base` returns). We read
        ``inputs.base_map_id``, ``run_id``, ``inputs.random_seed`` and
        ``finishability.route_verified`` out of it.
    parser : a :class:`SubprocessParser` with a working ``emit-map``
        command (GBX wrapper binary 0.2+; built via
        ``dotnet build -c Release`` on the ``gbx-wrapper`` project).
    output_dir : where the .Map.Gbx lands. Default is
        ``reports/generated-gbx/`` relative to cwd.

    Returns
    -------
    :class:`GbxEmitResult` with the new file's path + derived UID/name.

    Raises
    -------
    :class:`GbxEmitError` on any wrapper failure or missing base
    artifact. Runtime problems visible to the operator; not schema
    drift — if the generator produced a valid artifact but emission
    fails, it's always an environment issue (binary missing, base
    GBX missing, disk full).
    """
    base_map_id = artifact.get("inputs", {}).get("base_map_id")
    if base_map_id is None:
        raise GbxEmitError(
            "artifact.inputs.base_map_id is null — scratch generation "
            "has no base GBX to copy from (v0 doesn't support it)"
        )

    run_id = str(artifact.get("run_id") or "")
    if not run_id:
        raise GbxEmitError("artifact is missing run_id")
    seed = int(artifact.get("inputs", {}).get("random_seed", 0))
    verified = bool(artifact.get("finishability", {}).get("route_verified", False))

    base_title, base_path = _lookup_base_gbx(conn, int(base_map_id))

    new_map_uid = _derive_map_uid(run_id)
    new_map_name = _derive_map_name(
        base_title=base_title, run_id=run_id, seed=seed, verified=verified,
    )

    # Level-2 strip-to-route. A stripped artifact carries a *subset*
    # of the base map's grid blocks in map.blocks; forward their cells
    # as keep_cells so the C# wrapper drops the rest before Save.
    # Non-stripped artifacts (generation-v0 + stripped=false v0.1)
    # emit with keep_cells=None → wrapper no-ops the filter.
    map_block = artifact.get("map") or {}
    schema_version = str(artifact.get("schema_version") or "generation-v0")
    keep_cells: list[tuple[int, int, int]] | None = None
    ai_generated = bool(map_block.get("ai_generated") is True)

    if map_block.get("stripped") is True:
        keep_cells = []
        for b in map_block.get("blocks") or []:
            x, y, z = b.get("x"), b.get("y"), b.get("z")
            if x is None or y is None or z is None:
                continue
            keep_cells.append((int(x), int(y), int(z)))
        # Anchor cells ride along so a multi-cell CP doesn't lose the
        # cells the chosen route didn't step on.
        for cp in map_block.get("checkpoints") or []:
            x, y, z = cp.get("x"), cp.get("y"), cp.get("z")
            if x is None or y is None or z is None:
                continue
            keep_cells.append((int(x), int(y), int(z)))

    out_dir = output_dir or DEFAULT_GBX_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"base{base_map_id}-{run_id}.Map.Gbx"

    _LOG.info(
        "emit_gbx: base_map_id=%d run_id=%s seed=%d verified=%s "
        "schema=%s ai_generated=%s stripped=%s → %s",
        base_map_id, run_id, seed, verified, schema_version,
        ai_generated, keep_cells is not None, out_path,
    )

    if ai_generated:
        # v0.2 AI-generated: re-place every grid block from the
        # artifact's list. keep_cells is meaningless here — the
        # artifact IS the block list, not a filter over the base.
        result = parser.emit_map_from_blocks(
            base_path=base_path,
            output_path=out_path,
            map_uid=new_map_uid,
            map_name=new_map_name,
            blocks=list(map_block.get("blocks") or []),
        )
        if result.status is not ParseStatus.SUCCESS:
            raise GbxEmitError(
                f"wrapper emit-map-from-blocks failed: "
                f"code={result.error_code.value} detail={result.error_detail}"
            )
        payload = result.output or {}
        return GbxEmitResult(
            output_path=Path(str(payload.get("output_path") or out_path)),
            new_map_uid=str(payload.get("new_map_uid") or new_map_uid),
            new_map_name=new_map_name,
            base_path=base_path,
            block_count=int(payload.get("placed_block_count") or 0),
            baked_block_count=int(payload.get("baked_block_count") or 0),
            subprocess_duration_ms=result.duration_ms,
            source_block_count=int(payload.get("source_block_count") or 0),
            removed_block_count=int(payload.get("skipped_block_count") or 0),
            stripped=False,
        )

    result = parser.emit_map(
        base_path=base_path,
        output_path=out_path,
        map_uid=new_map_uid,
        map_name=new_map_name,
        keep_cells=keep_cells,
    )
    if result.status is not ParseStatus.SUCCESS:
        raise GbxEmitError(
            f"wrapper emit-map failed: code={result.error_code.value} "
            f"detail={result.error_detail}"
        )
    payload = result.output or {}

    return GbxEmitResult(
        output_path=Path(str(payload.get("output_path") or out_path)),
        new_map_uid=str(payload.get("new_map_uid") or new_map_uid),
        new_map_name=new_map_name,
        base_path=base_path,
        block_count=int(payload.get("block_count") or 0),
        baked_block_count=int(payload.get("baked_block_count") or 0),
        subprocess_duration_ms=result.duration_ms,
        source_block_count=int(payload.get("source_block_count") or 0),
        removed_block_count=int(payload.get("removed_block_count") or 0),
        stripped=keep_cells is not None,
    )


def emit_gbx_from_artifact_file(
    conn: Connection,
    *,
    artifact_path: Path,
    parser: SubprocessParser,
    output_dir: Path | None = None,
) -> GbxEmitResult:
    """Convenience wrapper that reads + parses the JSON file first."""
    with artifact_path.open("r", encoding="utf-8") as f:
        artifact = json.load(f)
    return emit_gbx_from_artifact(
        conn, artifact=artifact, parser=parser, output_dir=output_dir,
    )
