// Map-side emitter. Loads an existing .Map.Gbx, rewrites the
// identity-shaping fields (MapUid + MapName + optional author), and
// saves to a new path. This is the v0 "copy-from-base" shape from
// Phase 2 PR H: geometry is unchanged, but the emitted map is a
// distinct in-game entity so operators can load it side-by-side with
// the base and tell them apart.
//
// Level-2 (strip-to-route) extends THIS class with block filtering.
// Don't move the logic elsewhere — keeping it here means the emitter
// has exactly one public entry point and the PR H / Level-2 diff is
// localized.
//
// Input (stdin, one line of JSON):
//   {
//     "base_path":   "abs path to source .Map.Gbx",
//     "output_path": "abs path to write the new .Map.Gbx",
//     "map_uid":     "27-char base64 UID for the new map",
//     "map_name":    "display title for the new map"
//   }
//
// Output (stdout, wrapper protocol v1 envelope):
//   success → {"status":"success","parser_version":"x.y.z","output":{
//     "base_path": "...",
//     "output_path": "...",
//     "new_map_uid": "...",
//     "block_count": int,
//     "baked_block_count": int
//   }}
//   error   → {"status":"error", ...}  (ErrorCodes taxonomy)

using System.Text.Json;
using GBX.NET;
using GBX.NET.Engines.Game;

namespace TraxMania.GbxWrapper;

internal static class MapEmitter
{
    /// <summary>
    /// Read emit arguments from stdin as JSON, perform the emit, and
    /// return the output dict for the wrapper's "output" field.
    /// Throws on any failure; Program.cs classifies into the error
    /// taxonomy.
    /// </summary>
    public static Dictionary<string, object?> EmitFromStdinJson(string jsonLine)
    {
        if (string.IsNullOrWhiteSpace(jsonLine))
        {
            throw new InvalidDataException("emit-map: stdin JSON is empty");
        }

        EmitArgs args;
        try
        {
            args = JsonSerializer.Deserialize<EmitArgs>(jsonLine, JsonOpts)
                   ?? throw new InvalidDataException("emit-map: null JSON payload");
        }
        catch (JsonException ex)
        {
            throw new InvalidDataException($"emit-map: bad JSON: {ex.Message}");
        }

        if (string.IsNullOrWhiteSpace(args.BasePath))
            throw new InvalidDataException("emit-map: base_path is required");
        if (string.IsNullOrWhiteSpace(args.OutputPath))
            throw new InvalidDataException("emit-map: output_path is required");
        if (string.IsNullOrWhiteSpace(args.MapUid))
            throw new InvalidDataException("emit-map: map_uid is required");
        if (string.IsNullOrWhiteSpace(args.MapName))
            throw new InvalidDataException("emit-map: map_name is required");

        if (!File.Exists(args.BasePath))
            throw new FileNotFoundException($"base map missing: {args.BasePath}");

        // Ensure parent dir for output exists before we bother parsing
        // the source — cheap, and avoids Gbx.Save() blowing up on the
        // tail of a long operation.
        var outputDir = Path.GetDirectoryName(args.OutputPath);
        if (!string.IsNullOrEmpty(outputDir))
        {
            Directory.CreateDirectory(outputDir);
        }

        // Load as Gbx<T> so Save() round-trips the header/body pair.
        // ParseNode<T> drops the Gbx wrapper and we'd have to
        // reconstruct it; load the full Gbx<T> instead.
        var gbx = Gbx.Parse<CGameCtnChallenge>(args.BasePath)
                  ?? throw new InvalidDataException(
                      $"base parses but isn't a CGameCtnChallenge: {args.BasePath}");
        var map = gbx.Node
                  ?? throw new InvalidDataException(
                      $"base has no CGameCtnChallenge node: {args.BasePath}");

        // Rewrite identity. MapUid must be unique within the game's
        // collection or TM2020 treats it as a duplicate upload. 27-char
        // base64-url-safe matches the native TM2020 UID shape; we
        // accept whatever the caller passes so the Python side can
        // derive it deterministically from run_id (scope-v0
        // §Provenance).
        map.MapUid = args.MapUid;
        map.MapName = args.MapName;

        // Save back to disk.
        gbx.Save(args.OutputPath);

        return new Dictionary<string, object?>
        {
            ["base_path"] = args.BasePath,
            ["output_path"] = args.OutputPath,
            ["new_map_uid"] = map.MapUid,
            ["block_count"] = map.Blocks?.Count ?? 0,
            ["baked_block_count"] = map.BakedBlocks?.Count ?? 0,
        };
    }

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    };

    private sealed class EmitArgs
    {
        public string? BasePath { get; set; }
        public string? OutputPath { get; set; }
        public string? MapUid { get; set; }
        public string? MapName { get; set; }
    }
}
