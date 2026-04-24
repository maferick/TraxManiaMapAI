// Per-block footprint probe (#217-M1).
//
// Reads a single .Block.Gbx file (one block's definition) and emits
// the block-unit cell offsets its mesh actually occupies. These are
// the real footprints the strip policy needs: "PlatformPlasticWall-
// Straight4" occupies 4 cells, not 1, and the mesh extends from its
// origin along those cells — something the map's per-placement
// CGameCtnBlock record doesn't carry.
//
// Input (stdin, one line of text): absolute path to a .Block.Gbx.
//
// Output (stdout, wrapper protocol v1 envelope):
//   success → {"status":"success","parser_version":"x.y.z","output":{
//     "block_id":       "PlatformPlasticWallStraight4",
//     "ground_units":   [[dx,dy,dz], ...],   // relative unit offsets
//     "air_units":      [[dx,dy,dz], ...],
//     "ground_variant_count": int,
//     "air_variant_count":    int,
//     "has_ground":     bool,
//     "has_air":        bool
//   }}
//
// The ground/air split reflects GBX.NET's BlockInfoVariantGround +
// BlockInfoVariantAir — most blocks have both but some are ground-
// only (support / deco) or air-only (elevated tracks). For the
// Stripper's purposes we union the two unit sets and treat the
// block's footprint as the superset.

using System.Text.Json;
using System.Text.Json.Serialization;
using GBX.NET;
using GBX.NET.Engines.Game;

namespace TraxMania.GbxWrapper;

internal static class BlockInfoDump
{
    public static Dictionary<string, object?> DumpFromPath(string path)
    {
        if (!File.Exists(path))
            throw new FileNotFoundException($"block info file missing: {path}");

        // CGameCtnBlockInfo is an abstract base; the actual class is
        // one of the Classic / Flat / Slope / Road / Transition / etc.
        // concrete subtypes. Parse dynamically and validate that the
        // node derives from CGameCtnBlockInfo.
        var gbx = Gbx.Parse(path)
                  ?? throw new InvalidDataException(
                      $"couldn't parse {path} as a Gbx file");
        var info = gbx.Node as CGameCtnBlockInfo
                   ?? throw new InvalidDataException(
                       $"{path} is not a CGameCtnBlockInfo "
                       + $"(got {gbx.Node?.GetType().Name ?? "null"})");

        var groundUnits = ExtractUnits(info.GroundBlockUnitInfos);
        var airUnits = ExtractUnits(info.AirBlockUnitInfos);

        return new Dictionary<string, object?>
        {
            ["block_id"] = info.Ident?.Id,
            ["name"] = info.Name ?? info.Ident?.Id,
            ["collection"] = info.Ident?.Collection.ToString(),
            ["author"] = info.Ident?.Author,
            ["has_ground"] = groundUnits.Count > 0,
            ["has_air"] = airUnits.Count > 0,
            ["ground_units"] = groundUnits,
            ["air_units"] = airUnits,
            ["ground_variant_count"] =
                info.AdditionalVariantsGround?.Length + 1 ?? 1,
            ["air_variant_count"] =
                info.AdditionalVariantsAir?.Length + 1 ?? 1,
        };
    }

    // BlockUnitInfo arrays carry an int3 Coord per cell the block
    // occupies relative to its placement origin. Union-flatten into
    // a simple list of [dx, dy, dz] triples; dedupe preserves reader
    // sanity when a block has overlapping variants.
    private static List<int[]> ExtractUnits(
        IEnumerable<CGameCtnBlockUnitInfo>? units)
    {
        var seen = new HashSet<(int, int, int)>();
        var out_ = new List<int[]>();
        if (units is null) return out_;
        foreach (var u in units)
        {
            var c = u.RelativeOffset;
            var key = (c.X, c.Y, c.Z);
            if (seen.Add(key))
            {
                out_.Add(new[] { c.X, c.Y, c.Z });
            }
        }
        return out_;
    }
}
