# Corridor Diversity Metrics (A3)

## Why this exists

PRs #19–#20 + post-ingest diagnostics delivered a learned ranking
model that:

- improves AUC vs heuristic by +0.10–0.16 depending on cohort,
- but also narrows prediction stdev (stdev ratio ~0.39 in PR #19's
  first dry-run, moved to higher but still compressed values after
  A1/A2),
- and has its dominant variance-carrying feature flip between data
  regimes (path_length_log ↔ max_path_support_log).

That combination raises the question **"when ranking gets sharper,
does it collapse route variety?"**. The goal of A3 is to make that
question *measurable*, not handwaved.

A diversity metric isn't optional in a ranking-based mapper-assist
system. If the model starts preferring corridors that are too
similar, the downstream generator's only option becomes "pick one
of a set of near-identical shapes," which is a quiet failure mode
that AUC alone can't detect.

## Scope — what A3 ships

### Canonical similarity metric: Jaccard on `path_cells`

Given two corridors' `path_cells` (list of `(x, y, z)` tuples from
`route_corridors`), similarity is Jaccard:

```
J(A, B) = |cells(A) ∩ cells(B)| / |cells(A) ∪ cells(B)|
```

- Value in `[0, 1]`
- No free parameters
- Based on raw cell overlap — nothing rank-derived, nothing learned
- Honest: captures "how physically similar are these paths"
  without claiming anything about rank or quality

### Metrics built on top

1. **Within-interval similarity** — for each (map, interval) with
   ≥ 2 corridors, compute pairwise Jaccard between top-K corridors
   (default K=3). Per-interval diversity = 1 − mean_pairwise_J.
2. **Interval diversity distribution** — quartiles + histogram of
   per-interval diversity across all corridor-owning maps.
3. **Top-rank cross-map overlap** — for top-rank corridors
   (`path_rank = 0`) across all maps, show the pairwise Jaccard
   distribution. (Maps are different so overlap is expected to be
   low; this catches degenerate cases like "all top-rank corridors
   are short straight lines.")
4. **Virtual-edge concentration** — fraction of top-rank corridors
   that contain virtual edges. Extreme concentration in either
   direction is interesting: all-virtual → model over-relies on
   replay-observation bridges; all-grid → replay signal isn't
   reaching the ranker.
5. **Path-length spread** — stdev + range of top-rank path lengths
   globally and per-interval-size. Collapse in this dimension
   indicates the ranker is preferring one "canonical" length.

### Cross-ranker comparison (when present)

If the corridor table has both `corridor_confidence` (heuristic) and
`learned_corridor_score`, we can compute the *same* diversity metrics
over the top-K picked by each ranker. The delta is the answer to
"does learned ranking collapse variety relative to heuristic?".

## What A3 does NOT ship

- **Repeated-motif / n-gram detection.** Open-ended, many free
  parameters, motif research is its own project.
- **Diversity as a training loss term.** A3 *measures* diversity;
  using it to regularize training is A4 or later.
- **Normalizing diversity to account for map topology.** Some maps
  have only one sensible corridor. Trying to "correct" for that
  would add noise; we report raw numbers and let the reader interpret.

## Output

`python -m src.cli diagnose-corridor-diversity [--top-k K]
[--snapshot X] [--output path.md]`

Markdown report with sections:

- **Within-interval diversity** — distribution + worst-5 (most
  collapsed) intervals
- **Top-rank cross-map overlap** — distribution + flag if suspicious
- **Virtual-edge concentration** — percentage + histogram
- **Path-length spread** — distribution stats
- **Heuristic vs learned diversity** — if both scores present,
  side-by-side comparison of the top-K diversity per ranker

## Anti-leakage guardrails

- Similarity uses only `path_cells` (raw map geometry).
- Does NOT use: `path_rank`, `corridor_confidence`,
  `learned_corridor_score`, `path_support_count`, or any evidence
  feature. Those live on the *ranking* side of the comparison, not
  the *similarity* side.
- Top-K selection for cross-ranker comparison DOES use
  scores — but that's the object being measured, not the metric.

## What success looks like

- We can answer *"did learned ranking improve AUC by collapsing
  route variety?"* with numbers, not intuition.
- The report surfaces per-interval worst-cases so specific suspicious
  maps can be inspected.
- The metric is cheap enough to re-run after any ranking-model
  change without turning into a bottleneck.
