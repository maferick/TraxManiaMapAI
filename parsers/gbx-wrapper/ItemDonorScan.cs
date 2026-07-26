// scan-item-donors: find maps containing given item models.
//
// Items can only be placed by CLONING a structurally-complete anchored
// object out of a map that already uses that model (see MapBuilder for
// why assigning ItemModel corrupts an object). So placing trees needs a
// map that already has trees.
//
// Rather than hand-build one, mine the ingested corpus: ~25k real maps,
// many of which decorate with stock Nadeo vegetation. This scans them and
// reports which maps supply which models.
//
// Input (stdin, one JSON line):
//   {"root": "<dir of .Map.Gbx>", "models": ["SpringTreeMedium", ...],
//    "limit": 4000, "want_all": true}
//
// Output: per-model the first maps found supplying it, plus a ranked
// list of maps by how many of the requested models they cover — a single
// map covering everything is the ideal donor.

using System.Text.Json;
using GBX.NET;
using GBX.NET.Engines.Game;

namespace TraxMania.GbxWrapper;

internal static class ItemDonorScan
{
    private sealed record Request(
        string root, List<string> models, int? limit, bool? want_all,
        string? pattern);

    public static Dictionary<string, object?> ScanFromStdinJson(string json)
    {
        var req = JsonSerializer.Deserialize<Request>(json)
                  ?? throw new InvalidDataException("bad request JSON");
        if (!Directory.Exists(req.root))
            throw new DirectoryNotFoundException($"root not found: {req.root}");
        if (req.models is not { Count: > 0 })
            throw new InvalidDataException("models[] required");

        var wanted = new HashSet<string>(req.models, StringComparer.OrdinalIgnoreCase);
        var limit = req.limit ?? 5000;
        var wantAll = req.want_all ?? true;

        // model -> maps that contain a clonable object of it
        var suppliers = new Dictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);
        // map -> which wanted models it covers
        var coverage = new Dictionary<string, HashSet<string>>();

        int scanned = 0, parsed = 0, failed = 0;
        // Corpus artifacts are content-addressed with NO extension, so
        // the default matches everything and lets the parser decide.
        var pattern = string.IsNullOrWhiteSpace(req.pattern) ? "*" : req.pattern;
        foreach (var path in Directory.EnumerateFiles(
                     req.root, pattern, SearchOption.AllDirectories))
        {
            if (scanned >= limit) break;
            scanned++;
            CGameCtnChallenge? map;
            try { map = Gbx.ParseNode<CGameCtnChallenge>(path); }
            catch { failed++; continue; }
            if (map?.AnchoredObjects is not { Count: > 0 } objs) continue;
            parsed++;

            foreach (var o in objs)
            {
                // Same constraints MapBuilder needs from a donor.
                if (o.WaypointSpecialProperty is not null) continue;
                if (o.Chunks.Count < 3) continue;
                var id = o.ItemModel.Id.ToString();
                if (!wanted.Contains(id)) continue;

                if (!suppliers.TryGetValue(id, out var list))
                    suppliers[id] = list = new List<string>();
                if (list.Count < 3 && !list.Contains(path)) list.Add(path);

                if (!coverage.TryGetValue(path, out var set))
                    coverage[path] = set = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                set.Add(id);
            }

            if (wantAll && suppliers.Count >= wanted.Count) break;
        }

        var best = coverage
            .OrderByDescending(kv => kv.Value.Count)
            .Take(5)
            .Select(kv => new Dictionary<string, object?>
            {
                ["map"] = kv.Key,
                ["covers"] = kv.Value.OrderBy(x => x).ToList(),
                ["count"] = kv.Value.Count,
            })
            .ToList();

        return new Dictionary<string, object?>
        {
            ["scanned"] = scanned,
            ["with_items"] = parsed,
            ["parse_failures"] = failed,
            ["requested"] = req.models.Count,
            ["found"] = suppliers.Count,
            ["missing"] = wanted.Except(suppliers.Keys, StringComparer.OrdinalIgnoreCase)
                                .OrderBy(x => x).ToList(),
            ["suppliers"] = suppliers.ToDictionary(kv => kv.Key, kv => (object?)kv.Value),
            ["best_donors"] = best,
        };
    }
}
