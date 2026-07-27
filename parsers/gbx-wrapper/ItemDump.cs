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
    // Reflect the whole waypoint object, not just the fields we know
    // today. Tag is documented as free-form and Nadeo adds waypoint
    // types with client updates, so a fixed projection would silently
    // drop any new role. Storing the raw form lets a later pass extract
    // fields we cannot name yet, without reparsing 18,935 maps.
    // Takes object rather than the concrete waypoint type: GBX.NET does
    // not expose that type name under Engines.Game, and MapParser
    // likewise only ever pattern-matches it. Reflection needs no name.
    private static Dictionary<string, object?> DumpWaypoint(object wp)
    {
        var dict = new Dictionary<string, object?>();
        foreach (var prop in wp.GetType().GetProperties(
                     System.Reflection.BindingFlags.Public
                     | System.Reflection.BindingFlags.Instance))
        {
            try { dict[prop.Name] = prop.GetValue(wp)?.ToString(); }
            catch (Exception ex) { dict[prop.Name] = $"<throws: {ex.GetType().Name}>"; }
        }
        return dict;
    }

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

                // A waypoint-bearing item IS a checkpoint, finish or
                // spawn. On item-built maps the waypoints are items
                // rather than blocks, so this is what pins a route:
                // adding item anchors moved checkpoint-region coverage
                // from 0.0% to 65.6% on one pilot map and from 70% to
                // 100% on another, while barely moving surface
                // coverage. Reducing this to a boolean would discard
                // exactly the signal that made ingestion worthwhile.
                //
                // Tag is a free-form string, NOT an enum: TM2020 ships
                // at least Spawn / Goal / Checkpoint / LinkedCheckpoint
                // / StartFinish and Nadeo adds more with client
                // updates. Order is non-zero only on LinkedCheckpoint.
                ["waypoint_tag"] = obj.WaypointSpecialProperty?.Tag,
                ["waypoint_order"] = obj.WaypointSpecialProperty?.Order,
                ["waypoint_raw"] = obj.WaypointSpecialProperty is { } wp
                    ? DumpWaypoint(wp) : null,
            });
        }

        // BakedBlocks too. They are CGameCtnBlock like Blocks but live
        // in a separate collection and are equally absent from
        // block_placements. TM2020 puts the auto-generated terrain here,
        // and a car CAN drive on terrain, so an off-road stretch has no
        // row anywhere in the corpus. Emitted alongside items because
        // the question they answer is the same one: what is under the
        // car when block_placements says nothing is?
        // BakedBlocks are full CGameCtnBlock objects, not name+anchor
        // pairs, so emit the whole surface. Verified by reflection on
        // the installed GBX.NET rather than assumed from the file-format
        // docs: Coord, Direction, Flags, Variant, SubVariant, IsGround,
        // IsClip, IsGhost, IsPillar, WaypointSpecialProperty and a full
        // BlockModel Ident are all present.
        var baked = new List<Dictionary<string, object?>>();
        int bakedIndex = 0;
        foreach (var b in map.BakedBlocks ?? new List<CGameCtnBlock>())
        {
            var pos = b.AbsolutePositionInMap;
            baked.Add(new Dictionary<string, object?>
            {
                ["placement_index"] = bakedIndex++,
                ["name"] = b.Name,
                // Full identity. Name alone is ambiguous across
                // collections in the same way item ids are.
                ["model_id"] = b.BlockModel.Id.ToString(),
                ["model_collection"] = b.BlockModel.Collection.ToString(),
                ["model_author"] = b.BlockModel.Author,
                ["x"] = b.Coord.X,
                ["y"] = b.Coord.Y,
                ["z"] = b.Coord.Z,
                ["dir"] = (int)b.Direction,
                ["flags"] = b.Flags,
                ["variant"] = (int)b.Variant,
                ["sub_variant"] = (int)b.SubVariant,
                ["is_ground"] = b.IsGround,
                ["is_clip"] = b.IsClip,
                ["is_free"] = b.IsFree,
                ["is_ghost"] = b.IsGhost,
                ["is_pillar"] = b.IsPillar,
                ["abs_x"] = pos?.X,
                ["abs_y"] = pos?.Y,
                ["abs_z"] = pos?.Z,
                ["waypoint_tag"] = b.WaypointSpecialProperty?.Tag,
                ["waypoint_order"] = b.WaypointSpecialProperty?.Order,
                ["waypoint_raw"] = b.WaypointSpecialProperty is { } bwp
                    ? DumpWaypoint(bwp) : null,
            });
        }

        return new Dictionary<string, object?>
        {
            ["baked"] = baked,
            ["baked_total"] = baked.Count,
            ["map_uid"] = map.MapUid,
            ["title"] = map.MapName,
            ["blocks_total"] = map.Blocks?.Count ?? 0,
            ["items_total"] = items.Count,
            ["items"] = items,
        };
    }
}
