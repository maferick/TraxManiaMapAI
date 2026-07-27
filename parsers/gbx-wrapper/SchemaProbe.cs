// Reflect the property surface of the map and block types.
//
// Every API mistake this project has paid for came from assuming a
// member exists. Documentation and the chunkl schema describe the FILE
// format; what matters here is what the installed GBX.NET actually
// exposes as C# properties, which is a different question. This answers
// it once, cheaply, before any schema is written against it.
//
// Invoked as `<wrapper> probe-schema` with a map path on stdin.

using System.Reflection;

using GBX.NET;
using GBX.NET.Engines.Game;

namespace TraxMania.GbxWrapper;

internal static class SchemaProbe
{
    private static List<string> Props(Type t) =>
        t.GetProperties(BindingFlags.Public | BindingFlags.Instance)
            .Select(p => $"{p.Name}: {Pretty(p.PropertyType)}")
            .OrderBy(s => s)
            .ToList();

    private static string Pretty(Type t)
    {
        if (!t.IsGenericType) return t.Name;
        var args = string.Join(",", t.GetGenericArguments().Select(Pretty));
        return $"{t.Name.Split('`')[0]}<{args}>";
    }

    public static Dictionary<string, object?> ProbeFromPath(string path)
    {
        var map = Gbx.ParseNode<CGameCtnChallenge>(path)
                  ?? throw new InvalidDataException("not a CGameCtnChallenge");

        var result = new Dictionary<string, object?>
        {
            ["challenge_properties"] = Props(typeof(CGameCtnChallenge)),
            ["block_properties"] = Props(typeof(CGameCtnBlock)),
            ["anchored_properties"] = Props(typeof(CGameCtnAnchoredObject)),
        };

        // Live values for the structures we intend to ingest, so we can
        // tell "property absent" from "property present but null/empty"
        // on this particular map.
        var live = new Dictionary<string, object?>
        {
            ["blocks"] = map.Blocks?.Count,
            ["baked_blocks"] = map.BakedBlocks?.Count,
            ["anchored_objects"] = map.AnchoredObjects?.Count,
        };
        foreach (var name in new[]
                 { "BakedClipsAdditionalData", "BotPaths", "EmbeddedZipData",
                   "EmbeddedItemModels", "Embeds" })
        {
            var prop = typeof(CGameCtnChallenge).GetProperty(name);
            if (prop is null) { live[name] = "<no such property>"; continue; }
            object? v;
            try { v = prop.GetValue(map); }
            catch (Exception ex) { live[name] = $"<throws: {ex.GetType().Name}>"; continue; }
            if (v is null) { live[name] = null; continue; }
            live[name] = v is System.Collections.ICollection c
                ? $"{Pretty(v.GetType())} count={c.Count}"
                : $"{Pretty(v.GetType())} {(v is byte[] b ? $"bytes={b.Length}" : "")}";

            // First element's shape, so the table columns can be
            // written against something real.
            if (v is System.Collections.IEnumerable en and not string)
            {
                foreach (var first in en)
                {
                    if (first is null) break;
                    live[$"{name}__element"] = Props(first.GetType());
                    break;
                }
            }
        }
        result["live"] = live;
        return result;
    }
}
