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

    auto app = cast<CTrackMania>(GetApp());
    auto editor = cast<CGameCtnEditorFree>(app.Editor);
    if (editor is null) {
        UI::ShowNotification("Block Catalogue Dump",
            "Open the map editor first, then run the dump again.", 8000);
        g_running = false;
        return;
    }
    auto pmt = editor.PluginMapType;
    if (pmt is null || pmt.Inventory is null) {
        UI::ShowNotification("Block Catalogue Dump",
            "Editor inventory not available (PluginMapType is null).", 8000);
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

    auto inv = pmt.Inventory;
    log("root nodes: " + inv.RootNodes.Length);
    for (uint i = 0; i < inv.RootNodes.Length; i++) {
        WalkNode(inv.RootNodes[i], f);
    }

    f.Close();

    Json::Value done = Json::Object();
    done["schema"] = SCHEMA;
    done["blocks_dumped"] = g_dumped;
    done["articles_skipped"] = g_skipped;
    done["unix_time"] = Time::Stamp;
    Json::ToFile(donePath, done);

    log("done: " + g_dumped + " blocks dumped, " + g_skipped + " non-block articles skipped");
    UI::ShowNotification("Block Catalogue Dump",
        "Done: " + g_dumped + " blocks -> catalogue.ndjson", 10000);
    g_running = false;
}


void WalkNode(CGameCtnArticleNode@ node, IO::File@ f) {
    if (node is null) return;

    auto dir = cast<CGameCtnArticleNodeDirectory>(node);
    if (dir !is null) {
        for (uint i = 0; i < dir.ChildNodes.Length; i++) {
            WalkNode(dir.ChildNodes[i], f);
        }
        return;
    }

    auto art = cast<CGameCtnArticleNodeArticle>(node);
    if (art is null || art.Article is null) return;

    if (g_dumped % YIELD_EVERY_N_ARTICLES == 0) yield();

    CMwNod@ nod = art.Article.LoadedNod;
    if (nod is null) {
        art.Article.Preload();
        yield();
        @nod = art.Article.LoadedNod;
    }

    auto bi = cast<CGameCtnBlockInfo>(nod);
    if (bi is null) {
        // Items, macroblocks, folders-with-nothing — out of scope here.
        g_skipped++;
        return;
    }

    Json::Value rec = Json::Object();
    rec["type"] = "block";
    rec["id"] = bi.IdName;
    rec["name"] = string(bi.Name);
    rec["page"] = string(bi.PageName);
    rec["waypoint"] = int(bi.EdWaypointType);
    rec["is_road"] = bi.IsRoad;
    rec["is_terrain"] = bi.IsTerrain;
    rec["is_pillar"] = bi.IsPillar;
    rec["is_podium"] = bi.IsPodium;
    rec["no_respawn"] = bi.NoRespawn;

    Json::Value variants = Json::Array();
    AddVariant(variants, bi.VariantBaseGround, "ground", 0);
    AddVariant(variants, bi.VariantBaseAir, "air", 0);
    for (uint i = 0; i < bi.AdditionalVariantsGround.Length; i++) {
        AddVariant(variants, bi.AdditionalVariantsGround[i], "ground", int(i) + 1);
    }
    for (uint i = 0; i < bi.AdditionalVariantsAir.Length; i++) {
        AddVariant(variants, bi.AdditionalVariantsAir[i], "air", int(i) + 1);
    }
    rec["variants"] = variants;

    f.WriteLine(Json::Write(rec));
    g_dumped++;
    if (g_dumped % 200 == 0) log("dumped " + g_dumped + " blocks...");
}


void AddVariant(Json::Value@ arr, CGameCtnBlockInfoVariant@ v, const string &in kind, int index) {
    if (v is null) return;

    Json::Value jv = Json::Object();
    jv["kind"] = kind;
    jv["index"] = index;
    jv["size"] = Nat3(v.Size);

    Json::Value units = Json::Array();
    for (uint i = 0; i < v.BlockUnitInfos.Length; i++) {
        auto u = v.BlockUnitInfos[i];
        if (u is null) continue;
        Json::Value ju = Json::Object();
        ju["offset"] = Nat3(u.Offset);
        ju["rel_offset"] = Nat3(u.RelativeOffset);
        ju["underground"] = u.Underground;
        ju["terrain_modifier"] = u.TerrainModifierId.GetName();
        Json::Value clips = Json::Object();
        Json::Value cn = Json::Array();
        for (uint c = 0; c < u.Clips_North.Length; c++) {
            if (u.Clips_North[c] !is null) cn.Add(u.Clips_North[c].IdName);
        }
        clips["n"] = cn;
        Json::Value ce = Json::Array();
        for (uint c = 0; c < u.Clips_East.Length; c++) {
            if (u.Clips_East[c] !is null) ce.Add(u.Clips_East[c].IdName);
        }
        clips["e"] = ce;
        Json::Value cs = Json::Array();
        for (uint c = 0; c < u.Clips_South.Length; c++) {
            if (u.Clips_South[c] !is null) cs.Add(u.Clips_South[c].IdName);
        }
        clips["s"] = cs;
        Json::Value cw = Json::Array();
        for (uint c = 0; c < u.Clips_West.Length; c++) {
            if (u.Clips_West[c] !is null) cw.Add(u.Clips_West[c].IdName);
        }
        clips["w"] = cw;
        Json::Value ct = Json::Array();
        for (uint c = 0; c < u.Clips_Top.Length; c++) {
            if (u.Clips_Top[c] !is null) ct.Add(u.Clips_Top[c].IdName);
        }
        clips["top"] = ct;
        Json::Value cb = Json::Array();
        for (uint c = 0; c < u.Clips_Bottom.Length; c++) {
            if (u.Clips_Bottom[c] !is null) cb.Add(u.Clips_Bottom[c].IdName);
        }
        clips["bottom"] = cb;
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
