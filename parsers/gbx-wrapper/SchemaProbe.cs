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
        // Embedded ZIP inventory. A map carries the custom items it
        // uses, so this is the route to custom-item geometry that needs
        // no external download. Inventory only: no mesh parsing. The
        // question answered here is whether the .Item.Gbx assets the
        // map REFERENCES are actually PRESENT in its own zip, because
        // if they are not, the embedded route is a dead end.
        var zip = new Dictionary<string, object?>();
        var referenced = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var obj in map.AnchoredObjects ?? new List<CGameCtnAnchoredObject>())
        {
            var author = obj.ItemModel.Author ?? "";
            // Nadeo stock items are not embedded and would not be
            // expected in the zip; only community items matter here.
            if (author.Equals("Nadeo", StringComparison.OrdinalIgnoreCase)) continue;
            referenced.Add(obj.ItemModel.Id.ToString());
        }
        zip["referenced_custom_items"] = referenced.Count;

        var entries = new List<string>();
        if (map.EmbeddedZipData is { Length: > 0 } bytes)
        {
            zip["zip_bytes"] = bytes.Length;
            try
            {
                using var ms = new MemoryStream(bytes);
                using var archive = new System.IO.Compression.ZipArchive(
                    ms, System.IO.Compression.ZipArchiveMode.Read);
                foreach (var e in archive.Entries) entries.Add(e.FullName);
            }
            catch (Exception ex)
            {
                zip["zip_error"] = $"{ex.GetType().Name}: {ex.Message}";
            }
        }
        else
        {
            zip["zip_bytes"] = 0;
        }
        zip["entry_count"] = entries.Count;
        zip["item_gbx_count"] = entries.Count(
            e => e.EndsWith(".Item.Gbx", StringComparison.OrdinalIgnoreCase));
        zip["block_gbx_count"] = entries.Count(
            e => e.EndsWith(".Block.Gbx", StringComparison.OrdinalIgnoreCase));
        zip["entries_sample"] = entries.Take(6).ToList();

        // How many referenced custom items are actually resolvable in
        // the zip, matched on the entry's file stem.
        int resolved = 0;
        foreach (var id in referenced)
        {
            var stem = id.Replace('\\', '/').Split('/').Last();
            if (entries.Any(e => e.Replace('\\', '/').Split('/').Last()
                    .Equals(stem, StringComparison.OrdinalIgnoreCase)))
            {
                resolved++;
            }
        }
        zip["referenced_resolved_in_zip"] = resolved;
        result["embedded"] = zip;

        result["live"] = live;
        return result;
    }
}
