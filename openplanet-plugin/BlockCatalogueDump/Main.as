// Block Catalogue Dump — OpenPlanet plugin for TraxManiaMapAI.
//
// Dumps the game's authoritative block model for every block in the
// map-editor inventory: real multi-cell footprints (variant Size),
// per-unit occupancy offsets, and per-face clip lists (the game's own
// connector system). This replaces the block-name-regex classifier in
// src/constraints/block_geometry.py with ground truth, and is the
// prerequisite for the M2 multi-cell exit-direction-aware walker.
//
// Usage:
//   1. Open TM2020, enter the map editor (any empty map is fine).
//   2. Openplanet menu -> Plugins -> "Dump block catalogue".
//   3. Wait for the completion notification (a few minutes; the
//      inventory is walked with yields so the game stays responsive).
//
// Output (NDJSON, one JSON object per line):
//   <PluginStorage>/BlockCatalogueDump/catalogue.ndjson
//   First line is a meta record; then one record per inventory article.
//   A sibling catalogue.done.json is written on successful completion
//   so the ingestion side can tell a complete dump from an aborted one.
//
// The plugin only READS game state. It never places blocks, never
// modifies the map, and never removes files it didn't author.

const string PLUGIN_VERSION = "catalogue-dump-v0.1";
const string SCHEMA = "block_catalogue_v1";

// Yield cadence while walking articles. Keeps frame hitches invisible.
const int YIELD_EVERY_N_ARTICLES = 5;

bool g_running = false;
int g_dumped = 0;
int g_skipped = 0;
int g_failed = 0;
int g_nullArticle = 0;
int g_extracted = 0;
// Last field group touched by BuildBlockRecord — logged on exception
// so a Null pointer access names the offending API member.
string g_stage = "";


void RenderMenu() {
    if (UI::MenuItem("\\$8f0Dump block catalogue", "", false, !g_running)) {
        startnew(DumpCatalogue);
    }
}


void Main() {
    log("plugin version: " + PLUGIN_VERSION);
    log("output folder: " + IO::FromStorageFolder(""));
}


void DumpCatalogue() {
    if (g_running) return;
    g_running = true;
    g_dumped = 0;
    g_skipped = 0;
    g_failed = 0;

    // Enumerate via the game's file index (Fids) rather than the editor
    // inventory: the inventory tree only materialises articles the UI
    // has visited (a live run surfaced 75 of ~4000 blocks), while the
    // Fids tree lists every GameCtnBlockInfo the game ships. No editor
    // needed — this works from the main menu.
    CSystemFidsFolder@ root = Fids::GetGameFolder("GameData/Stadium/GameCtnBlockInfo");
    if (root is null) {
        UI::ShowNotification("Block Catalogue Dump",
            "Fids folder GameData/Stadium/GameCtnBlockInfo not found.", 8000);
        log("Fids::GetGameFolder returned null for GameData/Stadium/GameCtnBlockInfo");
        g_running = false;
        return;
    }

    string outPath = IO::FromStorageFolder("catalogue.ndjson");
    string donePath = IO::FromStorageFolder("catalogue.done.json");
    if (IO::FileExists(donePath)) IO::Delete(donePath);

    IO::File f(outPath, IO::FileMode::Write);

    Json::Value meta = Json::Object();
    meta["type"] = "meta";
    meta["schema"] = SCHEMA;
    meta["plugin_version"] = PLUGIN_VERSION;
    meta["unix_time"] = Time::Stamp;
    f.WriteLine(Json::Write(meta));

    log("fids root: " + root.Trees.Length + " subfolders, "
        + root.Leaves.Length + " files");

    // Iterative DFS — IO::File is a value type and cannot cross a
    // function boundary as a handle, so the walk stays in this scope.
    array<CSystemFidsFolder@> stack;
    stack.InsertLast(root);
    bool probed = false;

    while (stack.Length > 0) {
        CSystemFidsFolder@ cur = stack[stack.Length - 1];
        stack.RemoveLast();
        for (uint i = 0; i < cur.Trees.Length; i++) {
            if (cur.Trees[i] !is null) stack.InsertLast(cur.Trees[i]);
        }

        for (uint i = 0; i < cur.Leaves.Length; i++) {
            if ((g_dumped + g_failed + g_skipped) % YIELD_EVERY_N_ARTICLES == 0) yield();

            // Extract the raw Gbx alongside the runtime dump: clip
            // references are lazy (null handles) on file-loaded nods,
            // so clip names get parsed offline from these files via
            // the GBX.NET wrapper instead.
            try {
                if (Fids::Extract(cur.Leaves[i])) g_extracted++;
            } catch {}

            CMwNod@ nod = Fids::Preload(cur.Leaves[i]);
            auto bi = cast<CGameCtnBlockInfo>(nod);
            if (bi is null) {
                g_skipped++;
                continue;
            }

            // One-shot API probe on the first block: logs which clip
            // members are alive in this game build.
            if (!probed) {
                probed = true;
                ProbeClipApi(bi);
            }

            // One malformed block must never kill the dump: record the
            // failure and move on. Counted in the done marker.
            try {
                string line = BuildBlockRecord(bi);
                f.WriteLine(line);
                g_dumped++;
            } catch {
                g_failed++;
                log("record failed for '" + bi.IdName + "' at stage '" + g_stage
                    + "': " + getExceptionInfo());
            }
            if (g_dumped % 200 == 0) log("dumped " + g_dumped + " blocks...");
        }
    }

    f.Close();

    Json::Value done = Json::Object();
    done["schema"] = SCHEMA;
    done["blocks_dumped"] = g_dumped;
    done["articles_skipped"] = g_skipped;
    done["blocks_failed"] = g_failed;
    done["files_extracted"] = g_extracted;
    done["unix_time"] = Time::Stamp;
    Json::ToFile(donePath, done);

    log("done: " + g_dumped + " blocks dumped, " + g_skipped
        + " non-blockinfo files skipped, " + g_failed + " failed, "
        + g_extracted + " raw Gbx extracted");
    UI::ShowNotification("Block Catalogue Dump",
        "Done: " + g_dumped + " blocks (" + g_failed + " failed) -> catalogue.ndjson", 10000);
    g_running = false;
}


// One-shot diagnostic: which clip-related members are alive in this
// game build? Logged once per dump, on the first block encountered.
void ProbeClipApi(CGameCtnBlockInfo@ bi) {
    log("probe block: '" + bi.IdName + "'");
    CGameCtnBlockInfoVariant@ v = bi.VariantBaseGround;
    if (v is null) @v = bi.VariantBaseAir;
    if (v is null) { log("probe: no base variant"); return; }

    if (v.BlockUnitInfos.Length > 0 && v.BlockUnitInfos[0] !is null) {
        auto u = v.BlockUnitInfos[0];
        try { log("probe BlockUnitInfos[0].ClipCount_North = " + u.ClipCount_North); }
        catch { log("probe BlockUnitInfos[0].ClipCount_North threw"); }
        try { log("probe BlockUnitInfos[0].Clips_North.Length = " + u.Clips_North.Length); }
        catch { log("probe BlockUnitInfos[0].Clips_North threw"); }
        try {
            if (u.Clips_North.Length > 0) {
                log("probe Clips_North[0] is null: " + (u.Clips_North[0] is null));
                if (u.Clips_North[0] !is null) {
                    log("probe Clips_North[0].IdName = '" + u.Clips_North[0].IdName + "'");
                }
            }
        } catch { log("probe Clips_North[0] threw"); }
    } else {
        log("probe: BlockUnitInfos empty");
    }

    auto vg = cast<CGameCtnBlockInfoVariantGround>(bi.VariantBaseGround);
    if (vg !is null) {
        try {
            log("probe BlockUnitModels.Length = " + vg.BlockUnitModels.Length);
            if (vg.BlockUnitModels.Length > 0 && vg.BlockUnitModels[0] !is null) {
                auto m = vg.BlockUnitModels[0];
                try { log("probe BlockUnitModels[0].ClipCount_North = " + m.ClipCount_North); }
                catch { log("probe BlockUnitModels[0].ClipCount_North threw"); }
                try { log("probe BlockUnitModels[0].Clips_North.Length = " + m.Clips_North.Length); }
                catch { log("probe BlockUnitModels[0].Clips_North threw"); }
            }
        } catch {
            log("probe BlockUnitModels threw");
        }
    } else {
        log("probe: no ground variant to check BlockUnitModels on");
    }
}


string BuildBlockRecord(CGameCtnBlockInfo@ bi) {
    Json::Value rec = Json::Object();
    rec["type"] = "block";
    g_stage = "id"; rec["id"] = bi.IdName;
    g_stage = "name"; rec["name"] = string(bi.Name);
    g_stage = "page"; rec["page"] = string(bi.PageName);
    g_stage = "waypoint"; rec["waypoint"] = int(bi.EdWaypointType);
    g_stage = "flags";
    rec["is_road"] = bi.IsRoad;
    rec["is_terrain"] = bi.IsTerrain;
    rec["is_pillar"] = bi.IsPillar;
    rec["is_podium"] = bi.IsPodium;
    rec["no_respawn"] = bi.NoRespawn;

    Json::Value variants = Json::Array();
    g_stage = "variant_base_ground";
    AddVariant(variants, bi.VariantBaseGround, "ground", 0);
    g_stage = "variant_base_air";
    AddVariant(variants, bi.VariantBaseAir, "air", 0);
    g_stage = "variants_additional_ground";
    for (uint i = 0; i < bi.AdditionalVariantsGround.Length; i++) {
        AddVariant(variants, bi.AdditionalVariantsGround[i], "ground", int(i) + 1);
    }
    g_stage = "variants_additional_air";
    for (uint i = 0; i < bi.AdditionalVariantsAir.Length; i++) {
        AddVariant(variants, bi.AdditionalVariantsAir[i], "air", int(i) + 1);
    }
    rec["variants"] = variants;

    g_stage = "serialize";
    return Json::Write(rec);
}


void AddVariant(Json::Value@ arr, CGameCtnBlockInfoVariant@ v, const string &in kind, int index) {
    if (v is null) return;

    Json::Value jv = Json::Object();
    jv["kind"] = kind;
    jv["index"] = index;
    g_stage = "variant.size/" + kind;
    jv["size"] = Nat3(v.Size);

    Json::Value units = Json::Array();
    g_stage = "variant.unit_buffer/" + kind;
    for (uint i = 0; i < v.BlockUnitInfos.Length; i++) {
        auto u = v.BlockUnitInfos[i];
        if (u is null) continue;
        Json::Value ju = Json::Object();
        g_stage = "unit.offsets";
        ju["offset"] = Nat3(u.Offset);
        ju["rel_offset"] = Nat3(u.RelativeOffset);
        g_stage = "unit.underground";
        ju["underground"] = u.Underground;
        g_stage = "unit.terrain_modifier";
        ju["terrain_modifier"] = u.TerrainModifierId.GetName();
        g_stage = "unit.clips";
        Json::Value clips = Json::Object();
        // Clip buffers are dead members in some game builds (live run
        // 2026-07-25: Clips_* on BlockUnitInfos threw for every block).
        // Tolerate: a unit without readable clips still keeps its
        // footprint data, flagged with clips_error for the probe log.
        try {
        Json::Value cn = Json::Array();
        for (uint c = 0; c < u.Clips_North.Length; c++) {
            CGameCtnBlockInfoClip@ clip = u.Clips_North[c];
            if (clip !is null) cn.Add(clip.IdName);
        }
        clips["n"] = cn;
        Json::Value ce = Json::Array();
        for (uint c = 0; c < u.Clips_East.Length; c++) {
            CGameCtnBlockInfoClip@ clip = u.Clips_East[c];
            if (clip !is null) ce.Add(clip.IdName);
        }
        clips["e"] = ce;
        Json::Value cs = Json::Array();
        for (uint c = 0; c < u.Clips_South.Length; c++) {
            CGameCtnBlockInfoClip@ clip = u.Clips_South[c];
            if (clip !is null) cs.Add(clip.IdName);
        }
        clips["s"] = cs;
        Json::Value cw = Json::Array();
        for (uint c = 0; c < u.Clips_West.Length; c++) {
            CGameCtnBlockInfoClip@ clip = u.Clips_West[c];
            if (clip !is null) cw.Add(clip.IdName);
        }
        clips["w"] = cw;
        Json::Value ct = Json::Array();
        for (uint c = 0; c < u.Clips_Top.Length; c++) {
            CGameCtnBlockInfoClip@ clip = u.Clips_Top[c];
            if (clip !is null) ct.Add(clip.IdName);
        }
        clips["top"] = ct;
        Json::Value cb = Json::Array();
        for (uint c = 0; c < u.Clips_Bottom.Length; c++) {
            CGameCtnBlockInfoClip@ clip = u.Clips_Bottom[c];
            if (clip !is null) cb.Add(clip.IdName);
        }
        clips["bottom"] = cb;
        } catch {
            ju["clips_error"] = true;
        }
        ju["clips"] = clips;
        units.Add(ju);
    }
    jv["units"] = units;
    arr.Add(jv);
}


Json::Value Nat3(const nat3 &in v) {
    Json::Value arr = Json::Array();
    arr.Add(int(v.x));
    arr.Add(int(v.y));
    arr.Add(int(v.z));
    return arr;
}


void log(const string &in msg) {
    print("[BlockCatalogueDump] " + msg);
}
