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

No real evaluator logic exists yet. That is intentional — evaluators
land alongside the subsystems they grade (route inference in PR 5,
constraint graph in PR 6, full dry-run in PR 7).

## Conventions

- Each evaluator class sets `name` and `version` as class attributes.
- `version` is a semver string. See `docs/architecture.md` for the
  semantics of major/minor/patch bumps — they are load-bearing.
- `EvaluationResult` carries the provenance envelope (`created_at`,
  `code_version`, `source_artifact_ids`) required by `CLAUDE.md`.
- `EvaluationResult.created_at` must be timezone-aware. The
  `utcnow()` helper in `base.py` is the canonical source.
