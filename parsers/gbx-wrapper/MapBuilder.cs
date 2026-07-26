// emit-map-from-blocks: build a .Map.Gbx from a synthesised block list.
//
// The v0/v0.1 emit-map path (MapEmitter.cs) mutates a base map by
// filtering its existing blocks. The v0.2 AI generator synthesises
// a whole new block sequence that doesn't exist on any base map —
// filtering can't produce it. This emitter CLEARS the base's grid
// blocks and rebuilds them from the caller's list, keeping the
// base's environment / collection / lighting / decoration /
// BakedBlocks verbatim (those are the expensive things that would
// require building TM2020 map.gbx from true scratch — deferred).
//
// Input (stdin, one line of JSON):
//   {
//     "base_path":   "abs path to template .Map.Gbx (typically the
//                     AI run's base map — provides Stadium metadata)",
//     "output_path": "abs path to write",
//     "map_uid":     "27-char UID for the new map",
//     "map_name":    "display title",
//     "blocks": [
//       { "block_family": "Road", "block_name": "RoadTechStraight",
//         "x": 1, "y": 9, "z": 0, "rotation": 0 }, ...
//     ]
//   }
//
// Output (stdout, wrapper protocol v1 envelope):
//   success → {"status":"success","parser_version":"x.y.z","output":{
//     "base_path": "...", "output_path": "...",
//     "new_map_uid": "...",
//     "input_block_count": int,   // rows in the input list
//     "placed_block_count": int,  // blocks actually written
//     "skipped_block_count": int, // rows skipped (free-block rows etc.)
//     "baked_block_count": int    // untouched baked scenery from base
//   }}
//
// Free-placed blocks in the input JSON (identified by absence of
// grid coords) are skipped — v0.2 scope is grid-only per the
// minimal-ai-generator doc.

using System.Text.Json;
using System.Text.Json.Serialization;
using GBX.NET;
using GBX.NET.Engines.Game;

namespace TraxMania.GbxWrapper;

internal static class MapBuilder
{
    // Item geometry, measured from a hand-placed reference map
    // (5 trees on flat ground): absolute Y is 8 m at ground level
    // while the block-grid row for that same ground is 9.
    private const float GroundItemY = 8.0f;
    private const int GroundUnitY = 9;


    public static Dictionary<string, object?> BuildFromStdinJson(string jsonLine)
    {
        if (string.IsNullOrWhiteSpace(jsonLine))
            throw new InvalidDataException("emit-map-from-blocks: stdin JSON is empty");

        BuildArgs args;
        try
        {
            args = JsonSerializer.Deserialize<BuildArgs>(jsonLine, JsonOpts)
                   ?? throw new InvalidDataException(
                       "emit-map-from-blocks: null JSON payload");
        }
        catch (JsonException ex)
        {
            throw new InvalidDataException(
                $"emit-map-from-blocks: bad JSON: {ex.Message}");
        }

        if (string.IsNullOrWhiteSpace(args.BasePath))
            throw new InvalidDataException("emit-map-from-blocks: base_path required");
        if (string.IsNullOrWhiteSpace(args.OutputPath))
            throw new InvalidDataException("emit-map-from-blocks: output_path required");
        if (string.IsNullOrWhiteSpace(args.MapUid))
            throw new InvalidDataException("emit-map-from-blocks: map_uid required");
        if (string.IsNullOrWhiteSpace(args.MapName))
            throw new InvalidDataException("emit-map-from-blocks: map_name required");
        if (args.Blocks is null)
            throw new InvalidDataException("emit-map-from-blocks: blocks[] required");
        if (!File.Exists(args.BasePath))
            throw new FileNotFoundException($"base map missing: {args.BasePath}");

        var outputDir = Path.GetDirectoryName(args.OutputPath);
        if (!string.IsNullOrEmpty(outputDir))
            Directory.CreateDirectory(outputDir);

        var gbx = Gbx.Parse<CGameCtnChallenge>(args.BasePath)
                  ?? throw new InvalidDataException(
                      $"base isn't a CGameCtnChallenge: {args.BasePath}");
        var map = gbx.Node
                  ?? throw new InvalidDataException(
                      $"base has no CGameCtnChallenge node: {args.BasePath}");

        map.MapUid = args.MapUid;
        map.MapName = args.MapName;

        // Wipe the grid blocks. BakedBlocks + free-placed blocks are
        // left alone: BakedBlocks carry stadium scenery the v0.2
        // generator doesn't touch; free blocks carry anchor metadata
        // (Spawn / CP positions) that we can't safely rebuild from
        // grid coords alone. The input list's anchor rows land back
        // in map.Blocks via the Place loop below.
        if (map.Blocks is null)
        {
            throw new InvalidDataException(
                "base map has no Blocks collection; cannot rebuild");
        }
        int sourceBlockCount = map.Blocks.Count;
        var originalGridBlocks = new List<CGameCtnBlock>();
        foreach (var b in map.Blocks)
        {
            // Preserve free-placed blocks (CP/Goal anchors sometimes
            // materialise this way). Remove every grid block.
            if (!b.IsFree) originalGridBlocks.Add(b);
        }
        foreach (var b in originalGridBlocks) map.Blocks.Remove(b);

        // Re-place from the input list.
        int placed = 0;
        int skipped = 0;
        foreach (var entry in args.Blocks)
        {
            if (entry is null
                || string.IsNullOrWhiteSpace(entry.BlockName))
            {
                skipped++;
                continue;
            }
            // Grid-only per v0.2 scope. Rows without integer x/y/z
            // are either placeholder/free rows in the artifact (the
            // free anchors) or parser anomalies — skip silently.
            if (entry.X is null || entry.Y is null || entry.Z is null)
            {
                skipped++;
                continue;
            }
            var coord = new Int3(entry.X.Value, entry.Y.Value, entry.Z.Value);
            var direction = (Direction)(entry.Rotation & 0b11);
            var block = map.PlaceBlock(
                blockModel: entry.BlockName,
                coord: coord,
                direction: direction);
            if (entry.Variant is not null && block is not null)
            {
                block.Variant = entry.Variant.Value;
            }
            placed++;
        }

        // Scenery items.
        //
        // HOW ITEMS ACTUALLY WORK (established by elimination against the
        // game, because none of it is documented and GBX.NET hides it):
        //
        //   * PlaceAnchoredObject makes an object with only chunk
        //     0x03101002. Game-written objects also carry 0x03101004 and
        //     0x03101005, and GBX.NET cannot author 0x03101005 at all.
        //   * ASSIGNING ItemModel on an existing object makes that object
        //     unresolvable — the game reports it missing even when the
        //     value is a model the map already uses, and even when the
        //     assigned Ident was copied from a working object. The parsed
        //     properties end up identical to a working object, so the
        //     damage is in how the Ident re-serialises.
        //   * DUPLICATING a parsed object — new object, donor's chunk
        //     INSTANCES, donor's ItemModel — loads fine. The chunks carry
        //     the serialised model reference, so nothing is re-encoded.
        //
        // Therefore: never mutate a model, only clone. Which item models
        // we can place is bounded by what the donor map contains.
        int itemsPlaced = 0;
        int itemsSkipped = 0;
        var missingModels = new List<string>();
        if (args.Items is { Count: > 0 } && !string.IsNullOrWhiteSpace(args.ItemTemplatePath))
        {
            var template = Gbx.ParseNode<CGameCtnChallenge>(args.ItemTemplatePath)
                ?? throw new InvalidDataException(
                    $"item donor is not a map: {args.ItemTemplatePath}");

            // Index complete, non-waypoint donors by model id. Waypoint
            // objects carry start/finish metadata we must not clone.
            var byModel = new Dictionary<string, CGameCtnAnchoredObject>(
                StringComparer.OrdinalIgnoreCase);
            foreach (var o in template.AnchoredObjects ?? new List<CGameCtnAnchoredObject>())
            {
                if (o.WaypointSpecialProperty is not null) continue;
                if (o.Chunks.Count < 3) continue;
                var id = o.ItemModel.Id.ToString();
                if (id.Length > 0 && !byModel.ContainsKey(id)) byModel[id] = o;
            }
            if (byModel.Count == 0)
            {
                throw new InvalidDataException(
                    "item donor has no complete anchored objects: "
                    + args.ItemTemplatePath);
            }

            var placedObjects = new List<CGameCtnAnchoredObject>();
            foreach (var item in args.Items)
            {
                if (item is null || string.IsNullOrWhiteSpace(item.Name))
                {
                    itemsSkipped++;
                    continue;
                }
                if (!byModel.TryGetValue(item.Name, out var donor))
                {
                    // No donor for this model: skip it rather than emit a
                    // map the game will reject wholesale.
                    if (!missingModels.Contains(item.Name)) missingModels.Add(item.Name);
                    itemsSkipped++;
                    continue;
                }

                var copy = new CGameCtnAnchoredObject
                {
                    // Donor's own Ident instance, never rebuilt.
                    ItemModel = donor.ItemModel,
                    AbsolutePositionInMap = new Vec3(item.X, item.Y, item.Z),
                    YawPitchRoll = new Vec3(item.Yaw, item.Pitch, item.Roll),
                    Scale = donor.Scale == 0 ? 1f : donor.Scale,
                    Flags = donor.Flags,
                    PivotPosition = donor.PivotPosition,
                    AnchorTreeId = donor.AnchorTreeId,
                    Color = donor.Color,
                    BlockUnitCoord = new Byte3(
                        (byte)Math.Clamp((int)(item.X / 32.0f), 0, 255),
                        (byte)Math.Clamp(
                            GroundUnitY + (int)((item.Y - GroundItemY) / 8.0f),
                            0, 255),
                        (byte)Math.Clamp((int)(item.Z / 32.0f), 0, 255)),
                };
                // Share the donor's chunk instances: these hold the
                // serialised model reference that cannot be rebuilt.
                foreach (var ch in donor.Chunks) copy.Chunks.Add(ch);

                placedObjects.Add(copy);
                itemsPlaced++;
            }
            map.AnchoredObjects = placedObjects;
        }

        gbx.Save(args.OutputPath);

        return new Dictionary<string, object?>
        {
            ["base_path"] = args.BasePath,
            ["output_path"] = args.OutputPath,
            ["new_map_uid"] = map.MapUid,
            ["input_block_count"] = args.Blocks.Count,
            ["placed_block_count"] = placed,
            ["skipped_block_count"] = skipped,
            ["source_block_count"] = sourceBlockCount,
            ["baked_block_count"] = map.BakedBlocks?.Count ?? 0,
            ["placed_item_count"] = itemsPlaced,
            ["skipped_item_count"] = itemsSkipped,
            // Models the donor map does not contain, so they could not
            // be cloned. Surfaced so callers can fix their palette or
            // donor rather than wonder where the scenery went.
            ["item_models_without_donor"] = missingModels,
        };
    }

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    };

    private sealed class BuildArgs
    {
        public string? BasePath { get; set; }
        public string? OutputPath { get; set; }
        public string? MapUid { get; set; }
        public string? MapName { get; set; }
        public List<BuildBlockArg>? Blocks { get; set; }
        // Free-placed scenery items (trees, deco). Optional.
        public List<BuildItemArg>? Items { get; set; }
        /// <summary>
        /// Map to borrow structurally-complete anchored objects from.
        /// Required for items: see the retarget comment below.
        /// </summary>
        [JsonPropertyName("item_template_path")]
        public string? ItemTemplatePath { get; set; }
    }

    private sealed class BuildItemArg
    {
        /// <summary>Item id, e.g. "SpringTreeMedium".</summary>
        public string? Name { get; set; }
        /// <summary>Item author; defaults to "Nadeo" for stock items.</summary>
        public string? Author { get; set; }
        /// <summary>Item collection; defaults to the map's own.</summary>
        public string? Collection { get; set; }
        /// <summary>Absolute position in metres, not grid cells.</summary>
        public float X { get; set; }
        public float Y { get; set; }
        public float Z { get; set; }
        /// <summary>Radians. Yaw alone is enough for upright scenery.</summary>
        public float Yaw { get; set; }
        public float Pitch { get; set; }
        public float Roll { get; set; }
    }

    private sealed class BuildBlockArg
    {
        [JsonPropertyName("block_family")]
        public string? BlockFamily { get; set; }
        [JsonPropertyName("block_name")]
        public string? BlockName { get; set; }
        public int? X { get; set; }
        public int? Y { get; set; }
        public int? Z { get; set; }
        public int Rotation { get; set; }
        // Block variant index. Load-bearing for auto-pillar
        // replication: the game stacks TrackWallStraightPillar with
        // variant 0 at ground, 5 one above, 1 for the shaft. Null
        // leaves GBX.NET's default (0).
        public byte? Variant { get; set; }
    }
}
