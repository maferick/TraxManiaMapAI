#!/usr/bin/env bash
# End-to-end pipeline runner invoked by the Flask "Run Pipeline" action.
#
# Sequential by design — the 4 GB host can't safely parallelise
# heavy stages (see memory note `project_pipeline_memory_budget.md`).
# Each stage runs to completion before the next starts.
#
# Usage:
#   scripts/run_pipeline.sh                       # run over all snapshots
#   scripts/run_pipeline.sh --snapshot <id>       # scope to one snapshot
#
# Stages included (safe to re-run — each is idempotent or upsert-based):
#   parse-maps → build-graph → label-traversability → build-evidence →
#   update-path-support → update-pattern-weights → update-neg-evidence →
#   build-route-corridors → parse-replays → replay-clean → assign-cohorts →
#   score-route-corridors → score-corridors-learned (latest model JSON)
#
# Exit 0 on full success, non-zero if any stage fails. replay-clean's
# "partial" exit (non-zero on rejected replays) is tolerated via || true
# because a partial clean is a normal outcome, not a failure.
set -uo pipefail

SNAPSHOT_ARGS=()
if [[ "${1:-}" == "--snapshot" ]]; then
  SNAPSHOT_ARGS=(--snapshot "$2")
fi

cd "$(dirname "$0")/.."

run() {
  local stage="$1"; shift
  echo ""
  echo "=== $stage ==="
  python -m src.cli "$stage" "$@" || {
    local rc=$?
    # replay-clean returns 1 on "partial" status (some rejections) even
    # though no error happened. Swallow that specific non-failure.
    if [[ "$stage" == "replay-clean" && "$rc" == "1" ]]; then
      echo "  (replay-clean exited 1 — 'partial' status, not a failure)"
      return 0
    fi
    echo "stage '$stage' failed with exit $rc — aborting pipeline"
    return "$rc"
  }
}

run parse-maps "${SNAPSHOT_ARGS[@]}" || exit 1
run build-graph "${SNAPSHOT_ARGS[@]}" || exit 1
run label-traversability || exit 1
run build-traversability-evidence "${SNAPSHOT_ARGS[@]}" || exit 1
run update-path-support "${SNAPSHOT_ARGS[@]}" || exit 1
run update-pattern-weights || exit 1
run update-negative-evidence "${SNAPSHOT_ARGS[@]}" || exit 1
run build-route-corridors "${SNAPSHOT_ARGS[@]}" || exit 1
run parse-replays "${SNAPSHOT_ARGS[@]}" || exit 1
run replay-clean "${SNAPSHOT_ARGS[@]}" || exit 1
run assign-cohorts "${SNAPSHOT_ARGS[@]}" || exit 1
run score-route-corridors "${SNAPSHOT_ARGS[@]}" || exit 1

if [[ -f "reports/corridor-ranking-model-latest.json" ]]; then
  run score-corridors-learned --model-report "reports/corridor-ranking-model-latest.json" || exit 1
else
  echo ""
  echo "=== score-corridors-learned (skipped) ==="
  echo "reports/corridor-ranking-model-latest.json not found; use 'Train AI' first."
fi

echo ""
echo "=== pipeline done ==="
