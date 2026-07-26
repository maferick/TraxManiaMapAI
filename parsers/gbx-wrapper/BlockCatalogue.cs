// Offline block catalogue from extracted game files (#M2 prerequisite).
//
// Walks a folder of GameCtnBlockInfo Gbx files (produced by the
// BlockCatalogueDump OpenPlanet plugin via Fids::Extract) and emits an
// NDJSON catalogue: one record per block with real variant footprints
// and per-unit, per-face clip names. Clips are the game's own connector
// system — two blocks join when facing clips match — which the runtime
// dump cannot see (clip handles are lazy/null on file-loaded nods; live
// probe 2026-07-25).
//
// Input (stdin, one JSON line):
//   {"root": "<dir with extracted GameCtnBlockInfo tree>",
//    "out":  "<output dir; catalogue.ndjson + catalogue.done.json>"}
//
// Output envelope carries summary counts; the catalogue itself goes to
// files (it is ~15 MB — too big for the stdout protocol).

using System.Text.Json;
using GBX.NET;
using GBX.NET.Components;
using GBX.NET.Engines.Game;

namespace TraxMania.GbxWrapper;

internal static class BlockCatalogue
{
    private const string Schema = "block_catalogue_v1";

    private sealed record Request(string root, string @out);

    public static Dictionary<string, object?> DumpFromStdinJson(string json)
    {
        var req = JsonSerializer.Deserialize<Request>(json)
                  ?? throw new InvalidDataException("bad request JSON");
        if (!Directory.Exists(req.root))
            throw new DirectoryNotFoundException($"root not found: {req.root}");
        Directory.CreateDirectory(req.@out);

        var files = Directory.EnumerateFiles(req.root, "*.Gbx", SearchOption.AllDirectories)
            .OrderBy(p => p, StringComparer.Ordinal)
            .ToList();

        int blocks = 0, clips = 0, skipped = 0;
        var failures = new List<string>();

        var ndjsonPath = Path.Combine(req.@out, "catalogue.ndjson");
        using (var w = new StreamWriter(ndjsonPath))
        {
            var meta = new Dictionary<string, object?>
            {
                ["type"] = "meta",
                ["schema"] = Schema,
                ["source"] = "gbxnet_offline",
                ["parser_version"] = Program.ParserVersion,
                ["unix_time"] = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
            };
            w.WriteLine(JsonSerializer.Serialize(meta));

            foreach (var path in files)
            {
                try
                {
                    var node = Gbx.ParseNode(path);
                    if (node is CGameCtnBlockInfoClip clip)
                    {
                        w.WriteLine(JsonSerializer.Serialize(ClipRecord(clip, path)));
                        clips++;
                    }
                    else if (node is CGameCtnBlockInfo bi)
                    {
                        w.WriteLine(JsonSerializer.Serialize(BlockRecord(bi, path)));
                        blocks++;
                    }
                    else
                    {
                        skipped++;
                    }
                }
                catch (Exception ex)
                {
                    if (failures.Count < 25)
                        failures.Add($"{Path.GetFileName(path)}: {ex.GetType().Name}");
                    skipped++;
                }
            }
        }

        var done = new Dictionary<string, object?>
        {
            ["schema"] = Schema,
            ["source"] = "gbxnet_offline",
            ["blocks_dumped"] = blocks,
            ["clips_dumped"] = clips,
            ["articles_skipped"] = skipped,
            ["blocks_failed"] = failures.Count,
            ["unix_time"] = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
        };
        File.WriteAllText(Path.Combine(req.@out, "catalogue.done.json"),
            JsonSerializer.Serialize(done));

        return new Dictionary<string, object?>
        {
            ["catalogue_path"] = ndjsonPath,
            ["blocks"] = blocks,
            ["clips"] = clips,
            ["skipped"] = skipped,
            ["failures"] = failures,
        };
    }

    private static Dictionary<string, object?> BlockRecord(CGameCtnBlockInfo bi, string path)
    {
        var variants = new List<Dictionary<string, object?>>();
        AddVariant(variants, bi.VariantBaseGround, "ground", 0);
        AddVariant(variants, bi.VariantBaseAir, "air", 0);
        var extraGround = bi.AdditionalVariantsGround;
        if (extraGround is not null)
            for (int i = 0; i < extraGround.Length; i++)
                AddVariant(variants, extraGround[i], "ground", i + 1);
        var extraAir = bi.AdditionalVariantsAir;
        if (extraAir is not null)
            for (int i = 0; i < extraAir.Length; i++)
                AddVariant(variants, extraAir[i], "air", i + 1);

        return new Dictionary<string, object?>
        {
            ["type"] = "block",
            ["id"] = bi.Ident?.Id ?? BaseName(path),
            // Collection = environment (Stadium, BlueBay, ...). The
            // same block id exists in several collections with
            // different geometry, so consumers MUST disambiguate:
            // keying on id alone silently overwrites.
            ["collection"] = bi.Ident?.Collection.ToString() ?? "",
            ["name"] = bi.Name ?? "",
            ["page"] = bi.PageName ?? "",
            ["waypoint"] = bi.WayPointType.ToString(),
            ["is_pillar"] = bi.IsPillar,
            ["variants"] = variants,
        };
    }

    private static Dictionary<string, object?> ClipRecord(CGameCtnBlockInfoClip clip, string path)
    {
        return new Dictionary<string, object?>
        {
            ["type"] = "clip",
            ["id"] = clip.Ident?.Id ?? BaseName(path),
            ["name"] = clip.Name ?? "",
            ["page"] = clip.PageName ?? "",
        };
    }

    private static void AddVariant(
        List<Dictionary<string, object?>> variants,
        CGameCtnBlockInfoVariant? v, string kind, int index)
    {
        if (v is null) return;

        var units = new List<Dictionary<string, object?>>();
        int maxX = 0, maxY = 0, maxZ = 0;
        var models = v.BlockUnitModels;
        if (models is not null)
        {
            foreach (var u in models)
            {
                if (u is null) continue;
                var c = u.RelativeOffset;
                maxX = Math.Max(maxX, c.X);
                maxY = Math.Max(maxY, c.Y);
                maxZ = Math.Max(maxZ, c.Z);
                units.Add(new Dictionary<string, object?>
                {
                    ["offset"] = new[] { c.X, c.Y, c.Z },
                    ["underground"] = u.Underground,
                    ["terrain_modifier"] = u.TerrainModifierId ?? "",
                    ["surface"] = u.Surface ?? "",
                    ["dir"] = u.Dir.ToString(),
                    ["clips"] = new Dictionary<string, object?>
                    {
                        ["n"] = ClipNames(u.ClipsNorth),
                        ["e"] = ClipNames(u.ClipsEast),
                        ["s"] = ClipNames(u.ClipsSouth),
                        ["w"] = ClipNames(u.ClipsWest),
                        ["top"] = ClipNames(u.ClipsTop),
                        ["bottom"] = ClipNames(u.ClipsBottom),
                    },
                });
            }
        }

        variants.Add(new Dictionary<string, object?>
        {
            ["kind"] = kind,
            ["index"] = index,
            // GBX.NET does not expose the variant's Size field; the
            // occupied-cell bounding box from unit offsets is what the
            // walker needs anyway.
            ["size"] = new[] { maxX + 1, maxY + 1, maxZ + 1 },
            ["units"] = units,
        });
    }

    private static List<string> ClipNames(External<CGameCtnBlockInfoClip>[]? exts)
    {
        var names = new List<string>();
        if (exts is null) return names;
        foreach (var ext in exts)
        {
            var file = ext.File?.FilePath;
            if (!string.IsNullOrEmpty(file))
            {
                names.Add(BaseName(file));
                continue;
            }
            var id = ext.Node?.Ident?.Id;
            if (!string.IsNullOrEmpty(id)) names.Add(id);
        }
        return names;
    }

    // "...\RoadTechFC.EDClip.Gbx" -> "RoadTechFC"
    private static string BaseName(string path)
    {
        var name = Path.GetFileName(path);
        foreach (var suffix in new[] { ".Gbx", ".EDClip", ".EDClassic", ".EDFlat",
                                       ".EDPylon", ".EDFrontier" })
        {
            if (name.EndsWith(suffix, StringComparison.OrdinalIgnoreCase))
                name = name[..^suffix.Length];
        }
        var dot = name.IndexOf('.');
        return dot > 0 ? name[..dot] : name;
    }
}
