# What actually joins in TM2020 — measured, not assumed

Working notes for anything that generates maps, including the
description model. Every claim here was measured against the game or
the 18,935-map Stadium2020 corpus, and the ones that replaced a wrong
assumption say what the wrong assumption was.

## The three questions, and which source answers each

They get conflated constantly. They are not the same question, and no
single source answers more than one of them.

| Question | Source | Where |
|---|---|---|
| Does the game **permit** this geometry? | the editor | `tm_can_place` / `place_blocks` failures |
| Do the two surfaces **meet**? | clip data | `clip_matched` in `block_placement_grammar` |
| Do real mappers **do** it? | the corpus | `map_count` in `block_placement_grammar` |

**Acceptance is not connection.** Measured: the editor places a block
floating in mid-air with nothing touching it, and a block's
`MobilVariantIndex` is `0` whether it is joined end-to-end, sitting
side by side unjoined, or completely isolated. `CanPlaceBlock` is an
occupancy test. It will never tell you whether a car can drive across
the seam.

**Frequency is not validity, and validity is not quality.** A pair can
be permitted, clip-joined and common, and still make a bad track — see
"shape" below.

## Clip matching

The rule the generator ran on for months was "two blocks join iff
their route clips meet on the touching faces". That is **not** the
universal join rule, and it is also **not** dispensable. Both
over-corrections shipped broken maps.

What the corpus shows:

* Platform surfaces butt together with **mismatched clips**. Map 25192
  runs `PlatformTechStart` (`PlatformFCSmallRacing`) →
  `PlatformWaterSpecialTurbo2` (`PlatformWaterFCSmall`) →
  `PlatformTechToDecoWall` (`PlatFormFCSmall`). No two match.
* `PlatformTechStart` → `PlatformTechBase` is adjacent, unclipped, in
  **429 maps**.
* `PlatformPlasticStart` has **nine** successors and **zero** are
  clip-matched, while 97 maps place `PlatformPlasticBase` right after
  it. Platform gates attach without clips, always.
* The 1×4×1 `Gate*` arches (`GateCheckpoint`, `GateFinish`,
  `GateSpecial*`) have **no clips at all**. The corpus mounts them on
  pillar columns — `GateCheckpoint` shares its cell with
  `StructurePillar` in 353 maps — so no road block sits at offset
  (0,0,0) from one at any threshold.

And what happens if you therefore ignore clips: TM2020 draws a
**yellow-and-black dead-end barrier** at every road end that is not
joined. A route with 27% unclipped steps came out with 27 barriers in
it.

**The rule that works** is structural, and the corpus draws the line:

> If a block has *any* clip-matched continuation, use only those. If it
> has none, it is a gate, and unclipped is simply how it attaches.

Result by pool: dirt 0.0% unclipped steps, tech 0.6%, dirt+plastic
5.9%, plastic-only 12.3% — and what survives is almost entirely
`PlatformPlasticBase → PlatformPlasticCheckpoint`, the same
isolated-clip case.

## Surfaces cannot meet directly

There is **no** dirt-to-plastic block. No dirt-to-bump, no
platform-to-dirt either. The catalogue's whole transition graph:

| from | to | blocks |
|---|---|---|
| every `Platform<X>` | `RoadTech` | 3 each |
| every `Platform<X>` | `DecoWall` | 3 each |
| `RoadTech` | `RoadBump` | 14 |
| `RoadTech` | `RoadDirt` | 7 |
| `RoadTech` | `RoadIce` | 4 |

**`RoadTech` is the game's universal connector.** A pool that mixes
surfaces must contain it or the halves can never meet.

## Geometry the game enforces

Measured over 400 of the strongest grammar pairs, placed as isolated
pairs in a blank map:

* **396 of 400 accepted (99%).**
* **Zero disagreements** with the offline footprint/rotation model.
  That model had been wrong twice before; it is now corroborated at
  scale.
* Nothing can be placed below row 9. At ground level every pair with
  `dy < 0` is refused — that is the ground, not a pair rule. Probe
  above it.
* A route step must share a **face** with its predecessor, not an
  edge or a corner. Corner contact is what produced slabs meeting at
  their corners with nothing to drive across. Face contact is the
  right test rather than "axis-aligned", because a multi-cell block's
  anchor-to-anchor step is legitimately diagonal: a 1×1×2 source hands
  off at (1,0,1), the exact offset a 1×1 source must refuse.
* That same test rules out floating slabs for free — a 1×1 block
  cannot reach (0,+1,+1), a 1×2×1 ramp can, and that is exactly the
  difference between a ramp and a hovering tile.

## How much does the corpus actually add? A lot.

Two probe sets of 400 pairs each, same conditions:

| probe set | editor accepts | offline model disagrees |
|---|---|---|
| **attested** — the corpus contains them | **99.0%** | 0 |
| **unattested** — the corpus never does | **79.2%** | **12** |

**The game permits four fifths of the connections no mapper uses.**
Editor acceptance rules out roughly a fifth of nonsense and nothing
more, so it is a weak filter and the corpus is doing the real
filtering. Concretely, for a generator: never treat "the game let me
place it" as evidence that a pair belongs in a track.

The inverse is the useful direction. Attested pairs are accepted 99%
of the time, so a *refused* attested pair is almost always a bug in
our own model rather than a quirk of the corpus.

## Variant: a real gap, but NARROWER than first claimed

**Correction.** An earlier version of this document said the variant
gap was "proven" to explain every geometry disagreement, on the
evidence that the editor placed `PlatformPlasticCurve2` as
`variant=1, is_ground=false`. That inference was wrong. Checked
against the catalogue, that block's four variants — ground 0/1 and
air 0/1 — have **identical footprints**, so the variant the game chose
cannot explain anything about its shape.

What is actually true, measured across all 3,864 Stadium2020 blocks
with units:

| | blocks |
|---|---|
| ground[0] and air[0] have the **same** footprint | 3,769 |
| ground[0] and air[0] **differ** | 124 |
| several ground variants, all the **same** shape | 1,802 |
| several ground variants, **differing** shape | 92 |

Restricted to drivable surfaces, the exposure is **30 blocks** where
ground and air shapes differ and **45** whose ground variants differ —
and both sets are dominated by loop and `Slope2UBottom` geometry the
walker rarely reaches. So this is worth fixing and is **not** the
single highest-value item, which is what the previous version claimed.

The generator still assumes `variant("ground", 0)` everywhere while
the game picks per placement, so an elevated block gets a ground
footprint. For those ~75 drivable blocks that is a wrong shape.

### The disagreements, re-diagnosed

All twelve were recomputed by hand. Every one is a genuine cell
overlap under our model, which splits them into two groups:

* **"game refuses, model allows"** (10) — still unexplained. Our model
  finds no overlap and the game says no.
* **"game allows, model forbids"** (2) — our model finds a real
  overlap and the game accepted anyway. For example
  `RoadTechDiagRightCheckpoint` and `PlatformPlasticCurve2` at
  `(0,0,-1)` rel 2 share cells `(0,0,0)` and `(1,0,0)`.

The leading hypothesis for the second group is that **`PlaceBlock`
overwrites** rather than refusing, so both blocks report "placed"
while the first one is partly destroyed — which would also mean the
probe harness's "accepted" count is optimistic for overlapping pairs.
UNTESTED: it needs the editor open. `tools/probe_overwrite.py` runs
the experiment.

## Shape: legal is not a track

A route can be fully connected, non-overlapping and 100% accepted by
the game and still not look like a map. Measured over 201 corpus maps
(route blocks only, walls and pillars excluded):

| route levels over one XZ column | corpus |
|---|---|
| 1 | **95.2%** |
| 2 (a bridge) | 4.2% |
| 3 | 0.5% |
| 4 | 0.1% |

Median distinct columns per route block: **0.98**. Median route
bounding box: 351 cells at **0.21 blocks per box cell** — real routes
are sparse inside their own box; they wander.

An unconstrained walker put 48% of its columns at 2-4 levels, coiled
into a 20×5 patch. Capping a column at 2 passes and weighting reuse
down 20× reproduces the corpus: 95.6% / 4.4% / 0%.

## Vocabulary traps

* **Walls look exactly like road to a co-occurrence model.** A wall
  beside the road is as strong a neighbour as the road ahead. The
  corpus separates them: `PlatformPlasticCheckpoint`'s neighbours are
  `PlatformPlasticBase` with no wall in sight, while
  `PlatformPlasticWallStraight`'s are only more of itself at (0,±1,0)
  and (0,±2,0) — **walls stack vertically, road does not**. Growing
  the vocabulary out from waypoint blocks keeps them out; so does the
  clip-first rule, since `PlatFormWallStraightFC` never meets
  `PlatFormFCSmall`.
* **Take the top N *distinct targets*, not the top N rows.** A row is
  one (target, offset, rotation), and `PlatformPlasticBase`'s twelve
  strongest rows are all `PlatformPlasticBase` at twelve different
  offsets. A row-based cut never reaches `PlatformPlasticCurve1` — 31
  plastic corner blocks exist and none were buildable, which is why
  plastic sections came out as square-cornered slabs.
* **Gap pairs are mostly noise.** A real jump and two unrelated blocks
  three cells apart produce the identical row, and the second kind
  dominates: 81% of surviving rows in a smoke run were gaps. Keep them
  off by default and demand ~10× the breadth when enabled.

## Editor bridge gotchas

All measured the hard way; see `tools/tm_mcp/server.py`.

* `CanPlaceBlock` has **no short overload** — it needs `OnGround` and
  `VariantIndex` as well, unlike `PlaceBlock`.
* `CGameControlCameraEditorOrbital` has **no `m_TargetedDistance`**.
* `load_map` needs an **absolute path**; a relative one fails silently.
* `load_map` must wait for the old editor to close, or it reports
  success with the **previous** map still open.
* `clear` is **not** "empty the map" — `RemoveAllBlocks` only undoes
  edits made this session. On a loaded map the count comes straight
  back. Load a blank template instead.
* `can_place` is **useless at ground level**: the 48×48 terrain
  baseplate is 2304 `Grass` blocks occupying every ground cell, and
  `CanPlaceBlock` refuses to place over terrain while `PlaceBlock`
  replaces it happily.
* The editor's block list **includes** those 2304 `Grass` blocks.
* `data/catalogue/template48.Map.Gbx` is not empty — it carries 2
  stray `RoadTechStraight` blocks. Harmless for emitted maps because
  `emit-map-from-blocks` strips all grid blocks from the base, but it
  shows up in in-editor diffs.
* Validation cannot be automated: TM2020 requires a **human** to drive
  start to finish. `NotValidable` is still a genuine automated
  negative (rejected topology); `Validable` means the structure is
  accepted and it is waiting for a driver.

## Ordered runs: mined, and NOT yet usable as a weight

`block_route_sequences` holds ordered three-block runs from 3,636 of
4,000 maps (90.9%). The top patterns are real design, not noise:
`Straight x3` in 274 maps, **`SpecialTurbo2 x3` in 83 — boosters come
in runs**, `Curve1 -> Straight -> Straight` in 59, `SlopeStraight x3`
descending in 46 and ascending in 40,
`Straight -> Straight -> Checkpoint` in 46.

Two dead ends worth not repeating:

* **A Start->Finish shortest path is not a racing line.** On
  clip-matched links it reconstructed 2.8% of maps; on face contact
  19%, but those chains averaged 19 blocks because breadth-first
  short-circuits across a track that loops back near itself. The
  working extraction is **opposing-face triples**, which need no route
  order at all.
* **Feeding the triples in as a per-step weight makes maps worse.**
  Corpus baseline is 21 distinct route block types per map with the
  most-used block at 29% of the line. Generated routes already sit at
  14 / 0.31 — too repetitive before any prior — and the sequence prior
  moved both the wrong way at every weight (down to 11 / 0.40). The
  cause is structural: the strongest triples ARE same-block runs, so
  rewarding attested runs rewards repetition. Default is 0.

So the ordered evidence exists and is the only such evidence in the
project, but consuming it as a per-step weight is the wrong
granularity. `SEQUENCE_WEIGHT` is left in place at 0.

**The variety gap it was aimed at had a much cheaper cause.** Routes
carried 14 distinct block types against the corpus's 21 simply because
weighting by breadth lets the most popular blocks win every step.
Down-weighting a block used in the last 14 placements (`RECENCY_PENALTY`)
lands exactly on 21, with a top-block share of 0.23 against the
corpus's 0.29 — marginally more varied than real maps, which is the
safer side to miss on. The walker never lacked patterns to follow; it
lacked a reason not to repeat itself.

## Jumps, from the finishability axiom

Every corpus map was published and parses, so it can be driven —
therefore a gap its racing line MUST cross is drivable, and no physics
reasoning is required. A jump is **an open end facing another open end
across empty cells**: the line stops at a face, another stopped face
points back, nothing is in between, and the map completes.

Proximity was the failed definition — 81% of radius-3 grammar rows
were coincidence.

`block_jump_pairs`: 3,045 of 4,000 maps have jumps, 218,727
observations, 3,221 rows. Best attested:
`PlatformTechBase -> PlatformTechBase` across a 1-cell gap in 123 maps,
2-cell in 67; `PlatformPlasticBase` likewise. The classic platform gap
jump.

**Filter to drivable surfaces.** The first run's top "jump" was
`DecoPlatformBase -> DecoPlatformBase` in 323 maps — scattered
decoration whose open faces happen to point at each other. The support
filter caught `DecoWall` and not `DecoPlatform` / `DecoHill` /
`WaterBase`.

## Macros: mined, and the sampling constraint they impose

`block_macros` holds 3,395 recurring 4-8 block runs from 1,907 of
4,000 maps. Extraction chains through-pieces (a block whose neighbours
sit on opposing faces) and takes maximal chains, so no route order is
involved.

**88% of them are a single block repeated** — `PlatformTechBase x4`
in 212 maps, `RoadTechStraight x4` in 154. Only **397 (12%) are varied**,
and those are the actual design patterns:

| macro | maps |
|---|---|
| `PT.Base → PT.SpecialNoEngine → PT.SpecialNoEngine → PT.Base` | 20 |
| `PT.Base → PT.SpecialTurbo → PT.SpecialTurbo → PT.Base → PT.BaseWithHole24m → PT.SpecialNoEngine → …` | 18 |
| `PT.Diag1Slope2UpLeft → PT.Slope2Straight ×4` | 17 |
| `PT.Slope2Start → PT.SpecialNoEngine → PT.Curve2In → PT.Base → PT.SpecialTurbo` | 15 |

**The constraint this imposes on a planner: do not sample macros by
breadth.** The uniform runs outweigh the varied ones roughly ten to one
(212 against 20), so breadth-weighted sampling reproduces the same
repetition collapse that killed the sequence prior. A planner must
either sample the varied tail deliberately or weight within a
same-length, same-character bucket.

## Canonical order: geometry alone cannot give it

An autoregressive model over "next block" — and the planner below —
both need each corpus map as an ORDERED sequence. Three attempts, all
measured:

| method | result |
|---|---|
| clip-matched Start→Finish path | 2.8% of maps |
| face-contact Start→Finish shortest path | 19%, chains averaging 19 blocks — short-circuiting |
| chain-walk through degree-2 blocks | **not viable, see below** |

The third was the promising one: if a racing line is almost a path
graph, ordering is a walk rather than a search. Measured over 196 maps,
route blocks only:

| face-adjacent route neighbours | share |
|---|---|
| 0 | 5.9% |
| 1 | 7.0% |
| **2** | **23.6%** |
| 3 | 21.1% |
| **4** | **41.2%** |

Only a quarter of route blocks have degree 2 and **41% have degree
four** — because a platform field is a 2D grid where every tile touches
four others, and parallel road sections do the same. The graph is not
path-like, which is precisely why a shortest path had so much freedom
to cut corners.

Also worth knowing: only **126 of 196** maps have both a Start and a
Finish among catalogue-known blocks. The rest use community custom
gates or Multilap.

**So order is not recoverable from geometry.** It needs the driven
path — replay telemetry — which is the one unambiguous source and is
already the repo's stated contract (see CLAUDE.md, "Replay-ground-truth
learning"). The 20Hz telemetry plugin exists from PR #81 and has never
been run at scale. That is the dependency for any sequence model, and
for the planner.

## The planner, specified

Not built. The design, so it is not re-derived:

1. **Skeleton.** Anchor points across the map before any block is
   placed, spaced from the measured corpus geometry (median route
   bounding box 351 cells at 0.21 blocks per box cell). Checkpoints
   land on anchors instead of every N blocks.
2. **Segment intents.** Each anchor-to-anchor span gets a character
   (fast / technical / climb / gap), sampled to match corpus
   composition.
3. **Endpoint-directed fill.** THE hard part and the real dependency:
   the walker is a free DFS and cannot be told "get from A to B". It
   needs directed search over the grammar.
4. **Macro placement.** Fill spans with macros of matching character,
   sampled per the constraint above, falling back to block-by-block.
5. **Whole-route accept/reject** against the corpus metrics already
   measured here: 21 distinct types, 0.29 top-block share, 95.2%
   single-level columns, 351-cell box.

Steps 1, 2, 4 and 5 are straightforward. Step 3 is the work, and
everything else waits on it.

## What is still missing

1. **The planner** (specified above). Every whole-route property has
   so far been patched with a step-local hack — a column cap for
   spread, a boost plus reject-retry for surface mix, a recency
   penalty for variety. Those three work; pacing, checkpoint
   placement, jump run-ups and flow cannot be patched that way.
2. **`supports.py` over-generates.** Against the game on a 101-block
   route: 77 pillars match exactly, 24 more are emitted into free
   space the game leaves empty. None land inside the route, so nothing
   breaks, but the maps carry concrete a hand-built one would not.
3. **Nothing measures drivability.** Every check here is structural.
   Whether a route is actually *fun* or even completable needs the
   replay corpus, and the finishability gate still owns that.
4. **Junction blocks are used as through-pieces.** `RoadTechBranchTShaped`
   (3 clipped faces) and `*BranchCross` (4) get placed inline. Harmless
   — they are drivable straight through — but they read as stubs.
   Cannot be flagged by face count alone, because `PlatformPlasticBase`
   legitimately has 4 clipped faces.
