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

### Known gap: the grammar ignores block variant

The four refused pairs were all real overlaps:
`RoadTechStraight` at A-frame offset (1,0,1) from `RoadTechCurve2`
lands **inside** that block's 2×2 footprint. It cannot happen in a
real map — yet it is recorded in 175 maps.

The cause is that `block_placement_grammar` is keyed by
(block, block, offset, relative rotation) and **not by variant**,
while a block's footprint depends on its variant. `RoadTechCurve2`
ground variant 0 is 2×2; another variant is not.

Consequence today: ~1% of grammar rows are geometrically impossible.
The walker drops them silently via its occupancy check, so nothing is
broken — but the fix is to carry variant into the grammar key.

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

## What is still missing

1. **Variant in the grammar key** — see the gap above. Removes the
   impossible rows and would let the walker use air variants.
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
