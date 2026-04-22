# Replay Coverage Expansion Plan (A1)

Phase 4 v0.4 established that deeper replay coverage on
corridor-owning maps is the highest-leverage lever for improving
learned-ranking signal: 152→756 replays moved label stdev +51%,
prediction stdev +88%, and flipped evidence features from dormant
to dominant.

This plan formalizes the question *"where does the next replay
ingest go?"* into an **expected-value-to-learning** score so the
answer is data-driven, not intuition.

## Non-goals

- No claim that popular maps are better maps. Popularity is
  explicitly **not** a factor in the value score.
- No speculation about which maps "deserve" ingestion. The score
  measures expected signal change per additional replay, period.
- No actual ingestion. This document + CLI produce a plan; the
  ingest run is a separate step driven by the plan's output.

## Signals the score uses

All derived from existing tables. No new data collection.

1. **Corridor count per map** — a map contributes to learning only
   if it has enumerated corridors. Maps without corridors drop out
   of the value ranking entirely.
2. **Current clean replay count per map** — the denominator of
   *marginal* value. A map with 0 clean replays benefits enormously
   from the first one; a map with 20 benefits marginally from a 21st.
3. **Distance from TMX leaderboard cap** — TMX's
   `/api/replays/get_replays/{mapId}` returns at most 25 replays
   regardless of the `amount` parameter. Above that, additional
   ingest attempts are waste. See `SATURATION_PER_MAP` in
   `src/coverage/replay_value.py`.
4. **Cohort-threshold proximity** — maps whose current replay count
   is within 1–2 of a cohort-bucket boundary (config
   `cohorts.intent_lower_pct` / `intent_upper_pct` /
   `performance_top_pct` etc.) flip cohort assignment on one more
   replay. That's high leverage for the cohort-aware labels deferred
   to A2.

## Value formula (v0)

```
value(map) =
    1[corridor_count > 0]            # drops no-corridor maps
  * log(1 + corridor_count)          # log weighting: diminishing returns on corridor count
  * marginal_gain(clean_replays)     # highest when 0, 0 at saturation
  * (1 + cohort_threshold_bonus)     # small multiplicative nudge

marginal_gain(n) =
    if n >= SATURATION_PER_MAP:  0
    elif n == 0:                 1.0        (the big win)
    else:                        1 / sqrt(n + 1)

cohort_threshold_bonus =
    0.5 if clean_replays is within ±1 of any cohort-bucket boundary
    0.0 otherwise
```

This is a **first-cut heuristic**, not a fitted model. We're in
exploration mode — the goal is *directionally correct* ranking, not
optimal weighting. Iterate once we see real outputs from the ingest
batch.

## Saturation definition

A map is **saturated** if `clean_replays >= SATURATION_PER_MAP`
(currently 25). Saturated maps get `value = 0` and are excluded
from the backfill recommendation. The constant lives at the top of
`src/coverage/replay_value.py` with a comment explaining TMX's
leaderboard cap, so raising it is a one-line change if TMX ever
expands the endpoint.

## Cohort-threshold proximity

Cohort percentile thresholds are read from config at runtime
(`cohorts.intent_lower_pct`, `intent_upper_pct`,
`performance_top_pct`, `robustness_lower_pct`,
`robustness_upper_pct`). For each clean-replay count `n` on a map,
we compute how far `n` is from the nearest rank percentile that
would change the map's cohort-bucket membership. Within ±1 replay
of a boundary → flag as "threshold-adjacent" and give a modest
value bonus.

Anti-leakage: cohort percentile thresholds come from the `cohorts`
config section, not from any rank-derived feature. No feedback loop
from current learned scores.

## Report categories

The CLI emits a markdown report with these sections:

| Section | What it answers |
|---|---|
| **Saturated maps** | Can't pull more from TMX — ingestion wasted here |
| **Zero clean replays on corridor maps** | Highest marginal value per replay |
| **Cohort-threshold adjacent** | One more replay flips cohort bucket |
| **Top-N backfill recommendation** | Sorted by `value(map)`, descending |
| **Per-family counts** | Context — which block families are over- vs under-represented |

## What success looks like

- We can answer *"the next 500 replay ingests should target these
  maps"* with the report alone.
- The distribution of targets is **different** from the current
  "whatever had maps with any replay" distribution — specifically,
  more weight on zero-replay corridor maps.
- After running the recommended batch, re-running the diagnostic
  (`diagnose-corridor-ranking`) shows a further rise in time-envelope
  label stdev.

## What this does not ship

- No actual ingest run. A1 is planning + scoring, not execution.
- No cohort-aware label builder (A2).
- No diversity-collapse metric (A3).
- No learning-signals dashboard panel (A5, v0.2).

## Value-score iteration rules

The formula is v0. Revisions allowed when:

- We've run a backfill batch and measured label-stdev change.
- A signal we expected to matter (e.g., block-family diversity) is
  demonstrably missing from the current score.

Revisions **not** allowed:

- Adding popularity / award_count / view_count as a weight.
- Using learned-ranking outputs to weight future ingest.
- Hand-tuning per-map values outside the formula.
