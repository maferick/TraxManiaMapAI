// Dump every anchored object (item) placement in a map.
//
// Why this exists: block_placements covers CGameCtnBlock only. Items
// are CGameCtnAnchoredObject and were never ingested, so an item-built
// track has no rows to match telemetry against. Measured on the pilot:
// maps whose driven line is items scored 12-38% grounded coverage
// against 99-100% for block-built maps.
//
// The important discovery is that an item already carries
// BlockUnitCoord, its anchor cell in the same grid block_placements
// uses. Verified on a real item: AbsolutePositionInMap <767,18,858>
// maps to (767/32, 9+(18-8)/8, 858/32) = (23,10,26), exactly the
// reported BlockUnitCoord. So candidate generation needs no item mesh
// or geometry catalogue for a first pass, only the anchor.
//
// Emits one flat record per item. Deliberately does NOT try to resolve
// item geometry: that would mean parsing meshes or building an item
// catalogue, and the proof-of-value for that has to come first.
//
// Invoked as `<wrapper> dump-items` with the map path on stdin.

using GBX.NET;
using GBX.NET.Engines.Game;

namespace TraxMania.GbxWrapper;

internal static class ItemDump
{
    public static Dictionary<string, object?> DumpFromPath(string path)
    {
        var map = Gbx.ParseNode<CGameCtnChallenge>(path)
                  ?? throw new InvalidDataException("not a CGameCtnChallenge");

        var items = new List<Dictionary<string, object?>>();
        foreach (var obj in map.AnchoredObjects ?? new List<CGameCtnAnchoredObject>())
        {
            var pos = obj.AbsolutePositionInMap;
            var pyr = obj.PitchYawRoll;
            var cell = obj.BlockUnitCoord;
            var pivot = obj.PivotPosition;

            items.Add(new Dictionary<string, object?>
            {
                // Ident is the stable identity: (id, collection, author).
                // Author distinguishes a stock Nadeo item from a
                // community one embedded in the map's own zip.
                ["item_id"] = obj.ItemModel.Id.ToString(),
                ["collection"] = obj.ItemModel.Collection.ToString(),
                ["author"] = obj.ItemModel.Author,

                ["abs_x"] = pos.X,
                ["abs_y"] = pos.Y,
                ["abs_z"] = pos.Z,

                ["cell_x"] = (int)cell.X,
                ["cell_y"] = (int)cell.Y,
                ["cell_z"] = (int)cell.Z,

                ["pitch"] = pyr.X,
                ["yaw"] = pyr.Y,
                ["roll"] = pyr.Z,

                ["scale"] = obj.Scale,
                ["pivot_x"] = pivot.X,
                ["pivot_y"] = pivot.Y,
                ["pivot_z"] = pivot.Z,

                ["flags"] = obj.Flags,
                // A waypoint-bearing item IS a checkpoint or finish, so
                // it matters for route pinning, not just coverage.
                ["is_waypoint"] = obj.WaypointSpecialProperty is not null,
                ["snapped_on_block"] = obj.SnappedOnBlock?.ToString(),
                ["snapped_on_item"] = obj.SnappedOnItem?.ToString(),
            });
        }

        return new Dictionary<string, object?>
        {
            ["map_uid"] = map.MapUid,
            ["title"] = map.MapName,
            ["blocks_total"] = map.Blocks?.Count ?? 0,
            ["items_total"] = items.Count,
            ["items"] = items,
        };
    }
}
