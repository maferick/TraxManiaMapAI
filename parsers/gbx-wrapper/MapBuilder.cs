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
            map.PlaceBlock(
                blockModel: entry.BlockName,
                coord: coord,
                direction: direction);
            placed++;
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
    }
}
