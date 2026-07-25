# Block Catalogue Dump

Dumps the authoritative TM2020 block model straight from the game:
every block in the map-editor inventory with its real variant sizes,
per-unit cell offsets, and per-face clip lists. Clips are the game's
own connector system — two blocks join when their facing clips match —
so this file is the ground truth that replaces the block-name-regex
classifier in `src/constraints/block_geometry.py`.

## Install

Copy this folder to `%USERPROFILE%\OpenplanetNext\Plugins\BlockCatalogueDump\`
(requires OpenPlanet; developer plugin folders load on F3 -> Developer ->
Reload plugin, or restart the game).

## Run

1. Open TM2020, enter the map editor (an empty map is fine).
2. OpenPlanet menu -> Plugins -> **Dump block catalogue**.
3. Wait for the "Done: N blocks" notification.

## Output

`%USERPROFILE%\OpenplanetNext\PluginStorage\BlockCatalogueDump\`

- `catalogue.ndjson` — line 1 is a meta record, then one JSON object
  per block (schema `block_catalogue_v1`).
- `catalogue.done.json` — written only on successful completion, with
  the final block count. Ingestion must refuse a catalogue without it.

## Record shape

```json
{"type":"block","id":"RoadTechStraight","name":"...","page":"Roads/RoadTech",
 "waypoint":3,"is_road":true,"is_terrain":false,"is_pillar":false,
 "is_podium":false,"no_respawn":false,
 "variants":[
   {"kind":"ground","index":0,"size":[1,1,1],
    "units":[{"offset":[0,0,0],"rel_offset":[0,0,0],"underground":false,
              "terrain_modifier":"","clips":{"n":[],"e":["RoadTech"],
              "s":[],"w":["RoadTech"],"top":[],"bottom":[]}}]}]}
```

`waypoint` is the raw `EWayPointType` enum: 0=Start, 1=Finish,
2=Checkpoint, 3=None, 4=StartFinish, 5=Dispenser.

## Validation

After a dump, run from the repo root:

```bash
python tools/reverse_engineering/validate_block_catalogue.py \
  "%USERPROFILE%/OpenplanetNext/PluginStorage/BlockCatalogueDump/catalogue.ndjson"
```
