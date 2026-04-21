// Map-side parser. Produces a JSON-serializable dictionary for the
// "output" field of the wrapper protocol's success envelope.
//
// Shape:
//   {
//     "title":       string?,
//     "author":      string?,
//     "environment": string?,
//     "map_uid":     string?,
//     "has_items":   bool,
//     "is_block_mode": bool,
//     "blocks": [
//       { "name": string, "x": int, "y": int, "z": int,
//         "direction": string, "variant": int?, "sub_variant": int? },
//       ...
//     ]
//   }
//
// Field names mirror the Python-side expectations in
// src/ingestion/tmx.py::_normalize_summary and src/schema/maps.py so
// later wiring is a direct pass-through.

using GBX.NET;
using GBX.NET.Engines.Game;

namespace TraxMania.GbxWrapper;

internal static class MapParser
{
    public static Dictionary<string, object?> Parse(string path)
    {
        var map = Gbx.ParseNode<CGameCtnChallenge>(path)
                  ?? throw new InvalidDataException("file parses but is not a CGameCtnChallenge");

        var blocks = new List<Dictionary<string, object?>>();
        if (map.Blocks is not null)
        {
            foreach (var b in map.Blocks)
            {
                blocks.Add(new Dictionary<string, object?>
                {
                    ["name"] = b.Name,
                    ["x"] = b.Coord.X,
                    ["y"] = b.Coord.Y,
                    ["z"] = b.Coord.Z,
                    ["direction"] = b.Direction.ToString(),
                    ["variant"] = b.Variant,
                    ["sub_variant"] = b.SubVariant,
                });
            }
        }

        // Item count is a cheap proxy for has_items; later-stage parsing
        // may enrich this with actual anchored-object inspection.
        bool hasItems = map.AnchoredObjects is { Count: > 0 };

        return new Dictionary<string, object?>
        {
            ["title"] = map.MapName,
            ["author"] = map.AuthorLogin ?? map.AuthorNickname,
            ["environment"] = map.Collection?.ToString(),
            ["map_uid"] = map.MapUid,
            ["has_items"] = hasItems,
            ["is_block_mode"] = blocks.Count > 0,
            ["blocks"] = blocks,
        };
    }
}
