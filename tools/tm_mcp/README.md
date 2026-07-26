# TM Map Control — MCP bridge

Drive the TM2020 map editor from an MCP client. Two halves:

- `openplanet-plugin/TMMapControl/` — AngelScript plugin that executes
  commands against `CGameEditorPluginMap`
- `tools/tm_mcp/server.py` — MCP server that talks to it

## Why

Writing a `.Map.Gbx` directly (the GBX.NET path in `parsers/`) skips
the game's placement logic. Generated maps therefore arrive with **no
support pillars**, default block variants and no terrain adaptation.
Reproducing that externally means reverse-engineering variant tables
block type by block type — slow, and it breaks whenever Nadeo ships an
update.

Placing through the editor API makes the game do all of it: pillars,
variants, flags, terrain. Correct by construction.

The GBX writer is still the right tool for bulk/offline emission; this
bridge is for anything where the game's own judgement matters.

## Setup

1. Copy the plugin:

```bash
cp -r openplanet-plugin/TMMapControl "$USERPROFILE/OpenplanetNext/Plugins/"
```

2. In game: F3 → Developer → Reload plugin. (Signature mode must be
   Developer for unsigned local plugins.)

3. Install the MCP dependency:

```bash
pip install "mcp[cli]"
```

4. Register the server with your MCP client, e.g.:

```bash
claude mcp add trackmania -- python tools/tm_mcp/server.py
```

## Use

Open TM2020 and enter the **map editor** (any map). Then:

| Tool | Purpose |
| --- | --- |
| `tm_state` | editor open? which map? block count |
| `tm_clear_blocks` | wipe the open map |
| `tm_place_blocks` | place blocks; game adds pillars itself |
| `tm_save_map` | save (optionally rename) |
| `tm_validate_map` | run the editor's AI validation, get author time |

`tm_place_blocks` returns `blocks_total` — the map's block count after
placement. The gap between that and the number you sent is what the
game generated on its own.

## Protocol

File drop under `%USERPROFILE%\OpenplanetNext\PluginStorage\TMMapControl\`:

```
<id>.cmd.json   written by the MCP server
<id>.res.json   written by the plugin
```

No ports and no elevation, matching the existing `ai_rig_v1` telemetry
rig. The plugin never deletes a file it did not author; the server
cleans up its own pairs.

Override the location with `TM_MCP_STORAGE` if OpenPlanet is installed
elsewhere.
