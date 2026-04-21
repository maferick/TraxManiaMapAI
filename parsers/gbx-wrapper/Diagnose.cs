// Diagnostic mode: inspect the block / anchored-object structure of a
// real .Map.Gbx. Invoked as `<wrapper> diagnose-map` with the artifact
// path on stdin, same protocol as `map`/`replay`.
//
// Output: JSON with block counts, free/sentinel breakdown, anchored-
// object samples, and a reflection dump of all CGameCtnChallenge
// properties whose name contains "Mood" or "Decoration" (scenery
// ground-truthing).

using System.Reflection;

using GBX.NET;
using GBX.NET.Engines.Game;

namespace TraxMania.GbxWrapper;

internal static class Diagnose
{
    public static Dictionary<string, object?> Inspect(string path)
    {
        var map = Gbx.ParseNode<CGameCtnChallenge>(path)
                  ?? throw new InvalidDataException("not a CGameCtnChallenge");

        var result = new Dictionary<string, object?>
        {
            ["title"] = map.MapName,
            ["map_uid"] = map.MapUid,
            ["environment"] = map.Collection?.ToString(),
            ["blocks_total"] = map.Blocks?.Count ?? 0,
            ["baked_blocks_total"] = map.BakedBlocks?.Count ?? 0,
            ["anchored_objects_total"] = map.AnchoredObjects?.Count ?? 0,
            ["mood_candidates"] = ReflectMatching(map, new[] { "Mood", "Decoration", "Day", "Weather" }),
        };

        if (map.AnchoredObjects is { Count: > 0 })
        {
            var sample = new List<Dictionary<string, object?>>();
            foreach (var obj in map.AnchoredObjects.Take(3))
            {
                sample.Add(DumpProperties(obj));
            }
            result["anchored_object_sample"] = sample;
        }

        if (map.Blocks is not null)
        {
            InspectCollection(map.Blocks, result, prefix: "blocks");
        }
        if (map.BakedBlocks is not null)
        {
            InspectCollection(map.BakedBlocks, result, prefix: "baked_blocks");
        }

        return result;
    }

    private static Dictionary<string, object?> ReflectMatching(object target, string[] nameSubstrings)
    {
        var hits = new Dictionary<string, object?>();
        foreach (var prop in target.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance))
        {
            if (!nameSubstrings.Any(s => prop.Name.Contains(s, StringComparison.OrdinalIgnoreCase))) continue;
            object? value;
            try { value = prop.GetValue(target); }
            catch (Exception ex) { value = $"<throws: {ex.GetType().Name}>"; }
            hits[prop.Name] = value?.ToString();
        }
        return hits;
    }

    private static Dictionary<string, object?> DumpProperties(object target)
    {
        var dict = new Dictionary<string, object?>
        {
            ["_type"] = target.GetType().FullName,
        };
        foreach (var prop in target.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance))
        {
            object? value;
            try { value = prop.GetValue(target); }
            catch (Exception ex) { value = $"<throws: {ex.GetType().Name}>"; continue; }
            dict[prop.Name] = value?.ToString();
        }
        return dict;
    }

    private static void InspectCollection(
        IReadOnlyList<CGameCtnBlock> blocks,
        Dictionary<string, object?> into,
        string prefix)
    {
        int freeCount = 0;
        int sentinelCount = 0;
        var gridSamples = new List<Dictionary<string, object?>>();
        var freeSamples = new List<Dictionary<string, object?>>();
        var sentinelSamples = new List<Dictionary<string, object?>>();

        foreach (var b in blocks)
        {
            bool isFree = b.IsFree;
            bool isSentinel = b.Coord.X == -1 && b.Coord.Y == 0 && b.Coord.Z == -1;
            if (isFree) freeCount++;
            if (isSentinel) sentinelCount++;

            if (!isFree && !isSentinel && gridSamples.Count < 3)
            {
                gridSamples.Add(new Dictionary<string, object?>
                {
                    ["name"] = b.Name,
                    ["coord"] = new[] { b.Coord.X, b.Coord.Y, b.Coord.Z },
                    ["direction"] = b.Direction.ToString(),
                    ["variant"] = b.Variant,
                    ["sub_variant"] = b.SubVariant,
                    ["flags"] = b.Flags,
                });
            }
            if (isFree && freeSamples.Count < 3)
            {
                freeSamples.Add(new Dictionary<string, object?>
                {
                    ["name"] = b.Name,
                    ["abs_position"] = b.AbsolutePositionInMap is { } p
                        ? new[] { p.X, p.Y, p.Z }
                        : null,
                    ["yaw_pitch_roll"] = b.YawPitchRoll is { } r
                        ? new[] { r.X, r.Y, r.Z }
                        : null,
                    ["variant"] = b.Variant,
                });
            }
            if (isSentinel && !isFree && sentinelSamples.Count < 3)
            {
                sentinelSamples.Add(new Dictionary<string, object?>
                {
                    ["name"] = b.Name,
                    ["flags"] = b.Flags,
                    ["variant"] = b.Variant,
                });
            }
        }

        into[$"{prefix}_free_count"] = freeCount;
        into[$"{prefix}_sentinel_count"] = sentinelCount;
        into[$"{prefix}_sample_grid"] = gridSamples;
        into[$"{prefix}_sample_free"] = freeSamples;
        into[$"{prefix}_sample_sentinel"] = sentinelSamples;
    }
}
