// Map-side parser. Produces a JSON-serializable dictionary for the
// "output" field of the wrapper protocol's success envelope.
//
// Output shape:
//   {
//     "title":              string?,
//     "author":             string?,
//     "environment":        string?,
//     "map_uid":            string?,
//     "has_items":          bool,
//     "is_block_mode":      bool,
//     "baked_block_count":  int,
//     "blocks": [ <block>, ... ]
//   }
//
// <block> is either a grid-placed block or a free block; the
// "placement" field distinguishes them:
//
//   grid:
//     { "name": s, "placement": "grid",
//       "x": i, "y": i, "z": i,
//       "direction": s, "variant": b, "sub_variant": b, "flags": i }
//
//   free:
//     { "name": s, "placement": "free",
//       "abs_x": f, "abs_y": f, "abs_z": f,
//       "yaw": f, "pitch": f, "roll": f,
//       "variant": b, "sub_variant": b, "flags": i }
//
// GBX.NET marks free blocks with `IsFree=true` and stamps `Coord` with
// the sentinel (-1, 0, -1); the real position lives in
// `AbsolutePositionInMap` + `YawPitchRoll`. Emitting both kinds in one
// list with a discriminator keeps downstream consumers from having to
// re-run the branch themselves.
//
// `BakedBlocks` (stadium props — stands, grass, supports) is surfaced
// as a count only. Those are environment, not designable, and folding
// them into `blocks` would pollute the adjacency graph.

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
                blocks.Add(BlockToDict(b));
            }
        }

        bool hasItems = map.AnchoredObjects is { Count: > 0 };

        return new Dictionary<string, object?>
        {
            ["title"] = map.MapName,
            ["author"] = map.AuthorLogin ?? map.AuthorNickname,
            ["environment"] = map.Collection?.ToString(),
            ["map_uid"] = map.MapUid,
            ["has_items"] = hasItems,
            ["is_block_mode"] = blocks.Count > 0,
            ["baked_block_count"] = map.BakedBlocks?.Count ?? 0,
            ["blocks"] = blocks,
        };
    }

    private static Dictionary<string, object?> BlockToDict(CGameCtnBlock b)
    {
        var dict = new Dictionary<string, object?>
        {
            ["name"] = b.Name,
            ["variant"] = b.Variant,
            ["sub_variant"] = b.SubVariant,
            ["direction"] = b.Direction.ToString(),
            ["flags"] = b.Flags,
        };

        if (b.IsFree)
        {
            dict["placement"] = "free";
            if (b.AbsolutePositionInMap is { } p)
            {
                dict["abs_x"] = (double)p.X;
                dict["abs_y"] = (double)p.Y;
                dict["abs_z"] = (double)p.Z;
            }
            if (b.YawPitchRoll is { } r)
            {
                dict["yaw"] = (double)r.X;
                dict["pitch"] = (double)r.Y;
                dict["roll"] = (double)r.Z;
            }
        }
        else
        {
            dict["placement"] = "grid";
            dict["x"] = b.Coord.X;
            dict["y"] = b.Coord.Y;
            dict["z"] = b.Coord.Z;
        }

        return dict;
    }
}
