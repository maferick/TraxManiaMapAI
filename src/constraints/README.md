# src/constraints

Block adjacency / transition graph extraction. Not implemented yet.
Lands in PR 6.

## Key rule

**Do not treat low frequency as invalidity.** Rare transitions and illegal
transitions are different things. Edges carry evidence fields:

- observed in structurally valid maps
- observed in drivable maps
- observed in benchmark-quality maps
- replay-supported transition count

Evidence matters more than raw count.
