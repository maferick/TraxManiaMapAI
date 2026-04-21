# src/evaluation

Versioned evaluator scaffold. PR 2 lands the shape; concrete evaluators
arrive in PRs 5–7 and are graded against the frozen benchmark sets in
`data/benchmarks/`.

## Contents

| File             | Role                                                        |
|------------------|-------------------------------------------------------------|
| `base.py`        | `Evaluator` ABC and the `EvaluationResult` dataclass.       |
| `versioning.py`  | Semver parse/compare and the `VersionCompatibility` enum.   |
| `registry.py`    | Opt-in evaluator registration.                              |
| `evaluators/`    | Concrete evaluators (PR 7): `structural`, `adjacency_graph`, `route_coverage`. |
| `dryrun/`        | PR 7 dry-run runner + stats + markdown renderer.            |

## Dry-run (PR 7)

`python -m src.cli eval-benchmark` runs the evaluator stack over
benchmark manifests + an optional community sample and renders
`reports/evaluator-dryrun-v1.md`. The report pins evaluator and
benchmark versions, reports score distributions, benchmark rankings,
strong-vs-mediocre separation AUC, and disagreements. Scores are
persisted into `evaluation_artifacts` — the stored-vs-rendered split
lets later runs diff against history.

### What the shipped evaluators cover

- `structural_score` from both `StructuralEvaluator` (orphan proxy)
  and `AdjacencyGraphEvaluator` (Neo4j-labeled adjacency fraction)
- `drivability_score` from `RouteCoverageEvaluator` (extraction
  confidence on the latest route artifact)

Style, flow, and novelty stay `None` in PR 7 — those need trained
models outside Phase 1 scope.

## Conventions

- Each evaluator class sets `name` and `version` as class attributes.
- `version` is a semver string. See `docs/architecture.md` for the
  semantics of major/minor/patch bumps — they are load-bearing.
- `EvaluationResult` carries the provenance envelope (`created_at`,
  `code_version`, `source_artifact_ids`) required by `CLAUDE.md`.
- `EvaluationResult.created_at` must be timezone-aware. The
  `utcnow()` helper in `base.py` is the canonical source.
