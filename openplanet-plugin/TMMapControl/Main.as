// TM Map Control — editor automation endpoint for the MCP bridge.
//
// Lets an external process drive the TM2020 map editor: place blocks,
// save, clear, query state. Blocks placed through
// CGameEditorPluginMap go through the GAME's own placement path, so
// the game generates support pillars, picks block variants, applies
// flags and adapts terrain exactly as it does for a human mapper.
//
// That is the whole point. Writing a .Map.Gbx directly (our GBX.NET
// path) bypasses all of it, which is why generated maps came out with
// no pillars and why replicating them by hand turned into a long
// guessing game about variant indices.
//
// Protocol (file drop, same shape as the ai_rig_v1 telemetry rig):
//   <PluginStorage>/TMMapControl/<id>.cmd.json    caller writes
//   <PluginStorage>/TMMapControl/<id>.res.json    plugin writes
// The plugin never deletes a file it did not author.
//
// Commands:
//   {"op":"state"}
//   {"op":"clear"}
//   {"op":"place_blocks","blocks":[{"name":..,"x":..,"y":..,"z":..,
//                                   "dir":0..3}, ...]}
//   {"op":"save","name":"MyMap"}
//   {"op":"validate"}

const string PLUGIN_VERSION = "tm-map-control-v0.1";
const string PROTOCOL = "tm_mcp_v1";
const int SCAN_INTERVAL_MS = 400;

// Where placeable block definitions live. Index is built lazily on
// first placement so plugin load stays instant.
const string BLOCK_ROOT = "GameData/Stadium/GameCtnBlockInfo";

dictionary g_blockIndex;
bool g_indexed = false;
bool g_busy = false;


void Main() {
    log("version " + PLUGIN_VERSION);
    log("command folder: " + IO::FromStorageFolder(""));
    while (true) {
        yield();
        if (!g_busy) {
            array<string> pending = ScanPending();
            for (uint i = 0; i < pending.Length; i++) {
                ProcessCommand(pending[i]);
            }
        }
        sleep(SCAN_INTERVAL_MS);
    }
}


array<string> ScanPending() {
    array<string> out;
    array<string> entries = IO::IndexFolder(IO::FromStorageFolder(""), false);
    for (uint i = 0; i < entries.Length; i++) {
        string path = entries[i];
        if (!path.EndsWith(".cmd.json")) continue;
        string res = path.SubStr(0, path.Length - 9) + ".res.json";
        if (IO::FileExists(res)) continue;
        out.InsertLast(path);
    }
    return out;
}


void BuildBlockIndex() {
    if (g_indexed) return;
    CSystemFidsFolder@ root = Fids::GetGameFolder(BLOCK_ROOT);
    if (root is null) { g_indexed = true; return; }
    array<CSystemFidsFolder@> stack;
    stack.InsertLast(root);
    int n = 0;
    while (stack.Length > 0) {
        CSystemFidsFolder@ cur = stack[stack.Length - 1];
        stack.RemoveLast();
        for (uint i = 0; i < cur.Trees.Length; i++) {
            if (cur.Trees[i] !is null) stack.InsertLast(cur.Trees[i]);
        }
        for (uint i = 0; i < cur.Leaves.Length; i++) {
            CSystemFidFile@ leaf = cur.Leaves[i];
            if (leaf is null) continue;
            if (!leaf.FileName.EndsWith(".EDClassic.Gbx")) continue;
            string name = leaf.FileName.SubStr(0, leaf.FileName.Length - 14);
            g_blockIndex.Set(name, @leaf);
            n++;
        }
        if (n % 400 == 0) yield();
    }
    g_indexed = true;
    log("block index: " + n + " placeable blocks");
}


CGameCtnBlockInfo@ LookupBlock(const string &in name) {
    BuildBlockIndex();
    if (!g_blockIndex.Exists(name)) return null;
    CSystemFidFile@ fid = cast<CSystemFidFile@>(g_blockIndex[name]);
    if (fid is null) return null;
    return cast<CGameCtnBlockInfo>(Fids::Preload(fid));
}


void ProcessCommand(const string &in inPath) {
    g_busy = true;
    string outPath = inPath.SubStr(0, inPath.Length - 9) + ".res.json";

    Json::Value@ body = Json::FromFile(inPath);
    Json::Value res = Json::Object();
    res["protocol"] = PROTOCOL;
    res["plugin_version"] = PLUGIN_VERSION;

    if (body is null || body.GetType() != Json::Type::Object) {
        res["ok"] = false;
        res["error"] = "malformed command json";
        Json::ToFile(outPath, res);
        g_busy = false;
        return;
    }

    string op = string(body["op"]);
    res["op"] = op;

    auto app = cast<CTrackMania>(GetApp());
    auto editor = cast<CGameCtnEditorFree>(app.Editor);

    if (op == "state") {
        res["ok"] = true;
        res["editor_open"] = (editor !is null);
        if (editor !is null && editor.Challenge !is null) {
            res["map_name"] = editor.Challenge.MapName;
            res["blocks"] = int(editor.Challenge.Blocks.Length);
        }
    } else if (editor is null) {
        res["ok"] = false;
        res["error"] = "map editor is not open";
    } else if (op == "clear") {
        auto pmt = editor.PluginMapType;
        pmt.RemoveAllBlocks();
        res["ok"] = true;
        res["blocks"] = int(editor.Challenge.Blocks.Length);
    } else if (op == "place_blocks") {
        res = PlaceBlocks(editor, body, res);
    } else if (op == "save") {
        string name = string(body["name"]);
        auto pmt = editor.PluginMapType;
        if (name.Length > 0) editor.Challenge.MapName = name;
        pmt.SaveMap(editor.Challenge.MapName);
        res["ok"] = true;
        res["saved_as"] = editor.Challenge.MapName;
    } else if (op == "validate") {
        auto pmt = editor.PluginMapType;
        pmt.ValidateMap();
        yield();
        res["ok"] = true;
        res["validation_status"] = tostring(pmt.ValidationStatus);
        res["author_time_ms"] = editor.Challenge.TMObjective_AuthorTime;
    } else {
        res["ok"] = false;
        res["error"] = "unknown op '" + op + "'";
    }

    Json::ToFile(outPath, res);
    log("op=" + op + " -> " + (bool(res["ok"]) ? "ok" : string(res["error"])));
    g_busy = false;
}


Json::Value PlaceBlocks(
    CGameCtnEditorFree@ editor, Json::Value@ body, Json::Value res
) {
    auto pmt = editor.PluginMapType;
    Json::Value@ blocks = body["blocks"];
    if (blocks is null || blocks.GetType() != Json::Type::Array) {
        res["ok"] = false;
        res["error"] = "place_blocks needs a 'blocks' array";
        return res;
    }

    int placed = 0;
    int failed = 0;
    Json::Value failures = Json::Array();

    for (uint i = 0; i < blocks.Length; i++) {
        if (i % 25 == 0) yield();
        Json::Value@ b = blocks[i];
        string name = string(b["name"]);
        CGameCtnBlockInfo@ info = LookupBlock(name);
        if (info is null) {
            failed++;
            if (failures.Length < 20) failures.Add(name + " (unknown block)");
            continue;
        }
        int3 coord = int3(int(b["x"]), int(b["y"]), int(b["z"]));
        auto dir = CGameCtnBlock::ECardinalDirections(int(b["dir"]) & 3);
        bool ok = false;
        try {
            ok = pmt.PlaceBlock(info, coord, dir);
        } catch {
            ok = false;
        }
        if (ok) { placed++; }
        else {
            failed++;
            if (failures.Length < 20) {
                failures.Add(name + " @" + int(b["x"]) + ","
                    + int(b["y"]) + "," + int(b["z"]));
            }
        }
    }

    res["ok"] = (failed == 0);
    res["placed"] = placed;
    res["failed"] = failed;
    res["failures"] = failures;
    // Total block count AFTER placement — includes anything the game
    // generated itself (pillars in particular), which is the reason
    // this path exists.
    res["blocks_total"] = int(editor.Challenge.Blocks.Length);
    return res;
}


void log(const string &in msg) {
    print("[TMMapControl] " + msg);
}
