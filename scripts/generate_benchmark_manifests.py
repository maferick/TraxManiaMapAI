"""Generate tech-strong-proxy + tech-mediocre-proxy benchmark manifests.

These are NOT ground-truth benchmarks. They are auto-selected from the
ingested map distribution using two TMX-derived proxy signals:

- the TMX `Tech` style tag (self-reported by uploaders; noisy)
- TMX `AwardCount` (community upvotes — the v2 equivalent of the old
  star system; correlates with popularity more than with design quality)

Per docs/benchmark-policy.md, style/quality benchmarks must use
hand-curated labels. This generator exists to unblock the PR 7 evaluator
dry-run only, by providing input sets that are visually distinct enough
for separation-AUC to be interpretable. The manifests are named
`*-proxy` so the `tech-strong-v1` / `tech-mediocre-v1` slots stay
reserved for real hand-curated releases.

Usage:
    python scripts/generate_benchmark_manifests.py \\
        --snapshot 2026-04-scale-1k --config config/settings.yaml

Rewriting the files is safe: they have `released_at` set to today's
date, but the `-proxy` naming puts them outside the hand-curated
immutability contract. A follow-up commit can bump to v2 when the
generator output changes.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.utils.config import load_config  # noqa: E402
from src.storage.mariadb import cursor, open_connection  # noqa: E402

_LOG = logging.getLogger(__name__)

STRONG_BENCHMARK_ID = "tech-strong-proxy"
MEDIOCRE_BENCHMARK_ID = "tech-mediocre-proxy"
MANIFEST_VERSION = 1
STRONG_N = 20
MEDIOCRE_N = 20

_STRONG_RATIONALE = (
    "Auto-selected proxy set, NOT hand-curated. Pulled from the "
    "{snapshot} ingestion snapshot as the top-{n} TMX Tech-tagged maps by "
    "AwardCount (TMX v2 community-upvote count, the v2 equivalent of "
    "the old star system). This conflates popularity with quality and "
    "inherits tag noise from TMX self-reported tags. Used ONLY as input "
    "to the PR 7 evaluator dry-run to confirm that evaluators can "
    "separate visually distinct distributions at all; separation-AUC "
    "produced against this set is a sanity floor, not a production "
    "metric. The `tech-strong-v1` slot is reserved for a later "
    "hand-curated release that supersedes this proxy."
)
_MEDIOCRE_RATIONALE = (
    "Auto-selected proxy set, NOT hand-curated. Pulled from the "
    "{snapshot} ingestion snapshot as {n} TMX Tech-tagged maps with "
    "AwardCount = 0 (zero community upvotes), ordered deterministically "
    "by TMX id. 'Zero awards' means low popularity, not low quality — "
    "many of these are simply new or obscure. The proxy works for PR 7 "
    "dry-run only because it is distributionally disjoint from the "
    "strong-proxy set, so evaluators that can separate them are at "
    "least not degenerate. The `tech-mediocre-v1` slot is reserved for "
    "a hand-curated release that supersedes this proxy."
)


def _fetch_tech_maps(
    conn: Any, snapshot: str
) -> list[tuple[str, str, int]]:
    """Return (source_map_id, raw_artifact_hash, award_count) for parsed
    Tech-tagged maps in the given snapshot. Ordered by award_count DESC,
    then source_map_id ASC for deterministic tie-breaking.
    """
    with cursor(conn) as cur:
        cur.execute(
            """
            SELECT source_map_id, raw_artifact_hash, award_count
            FROM maps
            WHERE ingestion_snapshot = %s
              AND parse_status = 'success'
              AND raw_artifact_hash IS NOT NULL
              AND JSON_CONTAINS(style_tags_raw, %s)
            ORDER BY award_count DESC, CAST(source_map_id AS UNSIGNED) ASC
            """,
            (snapshot, '"Tech"'),
        )
        rows = cur.fetchall()
    return [(str(r[0]), str(r[1]), int(r[2] or 0)) for r in rows]


def _manifest_doc(
    *,
    benchmark_id: str,
    category: str,
    snapshot: str,
    rationale: str,
    released: date,
    author: str,
    entries: list[tuple[str, str, int]],
    comment_template: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "benchmark_id": benchmark_id,
        "version": MANIFEST_VERSION,
        "category": category,
        "ingestion_snapshot": snapshot,
        "released_at": released.isoformat(),
        "author": author,
        "rationale": rationale,
        "tags": ["proxy", "auto-generated", "phase1-dryrun"],
        "entries": [
            {
                "map_id": source_id,
                "content_hash": content_hash,
                "role": "primary",
                "label": {
                    "hand_curated": False,
                    "selection_signal": "tmx_award_count",
                    "award_count": award_count,
                },
                "comment": comment_template.format(award_count=award_count),
            }
            for source_id, content_hash, award_count in entries
        ],
    }


def _dump_yaml(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(
            doc,
            fh,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--snapshot", required=True, help="ingestion_snapshot to source maps from")
    p.add_argument("--author", required=True, help="email or handle for the released_at record")
    p.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / "data" / "benchmarks",
        help="where to write <benchmark_id>/<benchmark_id>-v<N>.yaml",
    )
    p.add_argument("--strong-n", type=int, default=STRONG_N)
    p.add_argument("--mediocre-n", type=int, default=MEDIOCRE_N)
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    conn = open_connection(cfg)
    try:
        rows = _fetch_tech_maps(conn, args.snapshot)
    finally:
        conn.close()

    if not rows:
        _LOG.error("no tech-tagged parsed maps found in snapshot %s", args.snapshot)
        return 1

    strong = rows[: args.strong_n]
    zero_awards = sorted(
        [r for r in rows if r[2] == 0],
        key=lambda r: int(r[0]),
    )
    if len(strong) < args.strong_n:
        _LOG.warning(
            "only %d tech maps available; strong set truncated to that count",
            len(strong),
        )
    if len(zero_awards) < args.mediocre_n:
        _LOG.error(
            "only %d zero-award tech maps; can't build a %d-entry mediocre set",
            len(zero_awards),
            args.mediocre_n,
        )
        return 1
    mediocre = zero_awards[: args.mediocre_n]

    strong_ids = {e[0] for e in strong}
    mediocre_ids = {e[0] for e in mediocre}
    overlap = strong_ids & mediocre_ids
    if overlap:
        _LOG.error("strong/mediocre sets overlap on ids: %s", sorted(overlap))
        return 1

    today = date.today()
    strong_doc = _manifest_doc(
        benchmark_id=STRONG_BENCHMARK_ID,
        category="strong_tech",
        snapshot=args.snapshot,
        rationale=_STRONG_RATIONALE.format(snapshot=args.snapshot, n=args.strong_n),
        released=today,
        author=args.author,
        entries=strong,
        comment_template="TMX AwardCount={award_count} at selection time.",
    )
    mediocre_doc = _manifest_doc(
        benchmark_id=MEDIOCRE_BENCHMARK_ID,
        category="mediocre_tech",
        snapshot=args.snapshot,
        rationale=_MEDIOCRE_RATIONALE.format(snapshot=args.snapshot, n=args.mediocre_n),
        released=today,
        author=args.author,
        entries=mediocre,
        comment_template="TMX AwardCount={award_count} at selection time.",
    )

    strong_path = (
        args.output_root
        / STRONG_BENCHMARK_ID
        / f"{STRONG_BENCHMARK_ID}-v{MANIFEST_VERSION}.yaml"
    )
    mediocre_path = (
        args.output_root
        / MEDIOCRE_BENCHMARK_ID
        / f"{MEDIOCRE_BENCHMARK_ID}-v{MANIFEST_VERSION}.yaml"
    )
    _dump_yaml(strong_path, strong_doc)
    _dump_yaml(mediocre_path, mediocre_doc)

    _LOG.info("wrote %s (%d entries)", strong_path, len(strong))
    _LOG.info("wrote %s (%d entries)", mediocre_path, len(mediocre))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
