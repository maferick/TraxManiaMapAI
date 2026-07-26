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
//   {"op":"load_map","map_file":"Maps/My Maps/x.Map.Gbx"}
//   {"op":"place_blocks","blocks":[{"name":..,"x":..,"y":..,"z":..,
//                                   "dir":0..3}, ...]}
//   {"op":"can_place","blocks":[...]}       legality, no mutation
//   {"op":"dump_blocks","filter":"RoadTech"}
//   {"op":"status"}                          validation status, no drive
//   {"op":"camera","x":..,"y":..,"z":..,"distance":..,
//    "h_angle":..,"v_angle":..}
//   {"op":"save","name":"MyMap"}
//   {"op":"validate"}

const string PLUGIN_VERSION = "tm-map-control-v0.2";
const string PROTOCOL = "tm_mcp_v1";
const int SCAN_INTERVAL_MS = 400;

// Where placeable block definitions live. Index is built lazily on
// first placement so plugin load stays instant.
const string BLOCK_ROOT = "GameData/Stadium/GameCtnBlockInfo";

// Ceiling on the AI validation drive. A short map settles in a few
// seconds; a long or broken one can sit at "Validable" indefinitely.
const int VALIDATE_WAIT_SECONDS = 180;
// Editor warm-up after EditMap(); first launch is the slow case.
const int EDITOR_OPEN_WAIT_SECONDS = 60;

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
                // An uncaught exception here would kill this
                // coroutine and leave g_busy stuck true, bricking the
                // plugin until reload. One bad command must not take
                // the endpoint down.
                try {
                    ProcessCommand(pending[i]);
                } catch {
                    log("command failed: " + getExceptionInfo());
                    WriteCrashResult(pending[i], getExceptionInfo());
                }
                g_busy = false;
            }
        }
        sleep(SCAN_INTERVAL_MS);
    }
}


array<string> ScanPending() {
    // NB: 'out' is a reserved keyword in AngelScript.
    array<string> pending;
    array<string> entries = IO::IndexFolder(IO::FromStorageFolder(""), false);
    for (uint i = 0; i < entries.Length; i++) {
        string path = entries[i];
        if (!path.EndsWith(".cmd.json")) continue;
        string res = path.SubStr(0, path.Length - 9) + ".res.json";
        if (IO::FileExists(res)) continue;
        pending.InsertLast(path);
    }
    return pending;
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

    if (op == "load_map") {
        // Open an existing .Map.Gbx in the editor so a generated
        // ARTIFACT can be validated, not just a route we re-place.
        // EditMap cannot transition cleanly from every state, so drop
        // to the menu first when we are not already idle.
        string mapFile = string(body["map_file"]);
        if (mapFile.Length == 0) {
            res["ok"] = false;
            res["error"] = "load_map needs 'map_file'";
        } else {
            if (editor !is null) {
                app.BackToMainMenu();
            }
            int waitUntil = Time::Stamp + 30;
            while (!app.ManiaTitleControlScriptAPI.IsReady
                   && Time::Stamp < waitUntil) {
                yield();
                sleep(200);
            }
            app.ManiaTitleControlScriptAPI.EditMap(mapFile, "", "");
            waitUntil = Time::Stamp + EDITOR_OPEN_WAIT_SECONDS;
            CGameCtnEditorFree@ opened = null;
            while (Time::Stamp < waitUntil) {
                yield();
                // Handle assignment needs '@' in AngelScript.
                @opened = cast<CGameCtnEditorFree>(app.Editor);
                if (opened !is null && opened.Challenge !is null) break;
                sleep(250);
            }
            res["ok"] = (opened !is null);
            if (opened !is null) {
                res["map_name"] = opened.Challenge.MapName;
                res["blocks"] = int(opened.Challenge.Blocks.Length);
            } else {
                res["error"] = "editor did not open '" + mapFile + "'";
            }
        }
    } else if (op == "state") {
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
    } else if (op == "can_place") {
        // The game's own legality test, WITHOUT mutating the map.
        //
        // This is the oracle the offline generator was missing. Every
        // structural bug so far — blocks meeting at a corner, a flat
        // tile stepping up, two road ends that do not join — is a
        // question the editor can answer directly, and answering it
        // per candidate is far cheaper than shipping a map and
        // looking at it.
        res = CanPlace(editor, body, res);
    } else if (op == "status") {
        // Read validation status WITHOUT calling Validate(). Validate()
        // parks the editor waiting for a human driver and then times
        // out; this just reports what the game already thinks, so it
        // is safe to call unattended after loading a map.
        auto pmt = editor.PluginMapType;
        res["ok"] = true;
        res["validation_status"] = VStatusToString(pmt.ValidationStatus);
        res["blocks"] = int(editor.Challenge.Blocks.Length);
        res["map_name"] = editor.Challenge.MapName;
    } else if (op == "camera") {
        res = MoveCamera(editor, body, res);
    } else if (op == "dump_blocks") {
        // Read the map back. This is what makes the editor usable as
        // an ORACLE for the offline emitter: place one block, dump,
        // and see exactly what the game generated around it.
        Json::Value arr = Json::Array();
        auto blocks = editor.Challenge.Blocks;
        string filter = body.HasKey("filter") ? string(body["filter"]) : "";
        for (uint i = 0; i < blocks.Length; i++) {
            auto b = blocks[i];
            if (b is null) continue;
            string name = b.BlockInfo !is null ? b.BlockInfo.IdName : "";
            if (filter.Length > 0 && name.IndexOf(filter) < 0) continue;
            Json::Value jb = Json::Object();
            jb["name"] = name;
            jb["x"] = b.Coord.x;
            jb["y"] = b.Coord.y;
            jb["z"] = b.Coord.z;
            jb["dir"] = int(b.Direction);
            // Runtime name differs from GBX.NET's 'Variant'.
            jb["variant"] = int(b.BlockInfoVariantIndex);
            jb["mobil_variant"] = int(b.MobilVariantIndex);
            jb["is_ground"] = b.IsGround;
            arr.Add(jb);
            if (i % 200 == 0) yield();
        }
        res["ok"] = true;
        res["count"] = int(arr.Length);
        res["blocks"] = arr;
    } else if (op == "save") {
        string name = string(body["name"]);
        auto pmt = editor.PluginMapType;
        if (name.Length > 0) editor.Challenge.MapName = name;
        pmt.SaveMap(editor.Challenge.MapName);
        res["ok"] = true;
        res["saved_as"] = editor.Challenge.MapName;
    } else if (op == "validate") {
        auto pmt = editor.PluginMapType;
        // NOT automated: TM2020 validation needs a HUMAN to drive the
        // map start to finish, and that run sets the author time.
        // There is no AI driver (corrected 2026-07-26 — earlier
        // comments here and in AIRouteTelemetry claimed otherwise).
        //
        // Status walks NotValidable -> Validable -> (someone drives)
        // -> Validated. So an unattended call settles at "Validable"
        // and then times out waiting for a driver. "NotValidable" is
        // still a genuine automated signal: it means the topology is
        // wrong (no Start/Finish, unlinked CPs).
        pmt.Validate();
        int deadline = Time::Stamp + VALIDATE_WAIT_SECONDS;
        bool settled = false;
        while (Time::Stamp < deadline) {
            yield();
            auto st = pmt.ValidationStatus;
            if (st == CGameEditorPluginMapMapType::EValidationStatus::Validated
                || st == CGameEditorPluginMapMapType::EValidationStatus::NotValidable) {
                settled = true;
                break;
            }
            sleep(500);
        }
        bool validated = (pmt.ValidationStatus
            == CGameEditorPluginMapMapType::EValidationStatus::Validated);
        res["ok"] = true;
        res["settled"] = settled;
        res["validated"] = validated;
        res["validation_status"] = VStatusToString(pmt.ValidationStatus);
        res["author_time_ms"] = validated
            ? int(editor.Challenge.TMObjective_AuthorTime) : -1;
    } else {
        res["ok"] = false;
        res["error"] = "unknown op '" + op + "'";
    }

    Json::ToFile(outPath, res);
    // NB: do not read res["error"] here — it is absent on success and
    // on partial failures (place_blocks reports 'failures' instead),
    // and stringifying a missing key throws.
    log("op=" + op + " ok=" + (bool(res["ok"]) ? "true" : "false"));
    g_busy = false;
}


// Always leave a response file: a caller blocked on <id>.res.json
// should get an error, not a timeout.
void WriteCrashResult(const string &in inPath, const string &in info) {
    string outPath = inPath.SubStr(0, inPath.Length - 9) + ".res.json";
    if (IO::FileExists(outPath)) return;
    Json::Value res = Json::Object();
    res["protocol"] = PROTOCOL;
    res["plugin_version"] = PLUGIN_VERSION;
    res["ok"] = false;
    res["error"] = "plugin exception: " + info;
    Json::ToFile(outPath, res);
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
        auto dir = CGameEditorPluginMap::ECardinalDirections(int(b["dir"]) & 3);
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


Json::Value CanPlace(
    CGameCtnEditorFree@ editor, Json::Value@ body, Json::Value res
) {
    auto pmt = editor.PluginMapType;
    Json::Value@ blocks = body["blocks"];
    if (blocks is null || blocks.GetType() != Json::Type::Array) {
        res["ok"] = false;
        res["error"] = "can_place needs a 'blocks' array";
        return res;
    }

    Json::Value arr = Json::Array();
    int legal = 0;
    for (uint i = 0; i < blocks.Length; i++) {
        if (i % 25 == 0) yield();
        Json::Value@ b = blocks[i];
        string name = string(b["name"]);
        Json::Value entry = Json::Object();
        entry["name"] = name;
        entry["x"] = int(b["x"]);
        entry["y"] = int(b["y"]);
        entry["z"] = int(b["z"]);
        entry["dir"] = int(b["dir"]) & 3;

        CGameCtnBlockInfo@ info = LookupBlock(name);
        if (info is null) {
            entry["can_place"] = false;
            entry["reason"] = "unknown block";
        } else {
            int3 coord = int3(int(b["x"]), int(b["y"]), int(b["z"]));
            auto dir = CGameEditorPluginMap::ECardinalDirections(
                int(b["dir"]) & 3);
            bool ok = false;
            try {
                ok = pmt.CanPlaceBlock(info, coord, dir);
            } catch {
                ok = false;
                entry["reason"] = "exception: " + getExceptionInfo();
            }
            entry["can_place"] = ok;
            if (ok) legal++;
        }
        arr.Add(entry);
    }
    res["ok"] = true;
    res["legal"] = legal;
    res["checked"] = int(arr.Length);
    res["results"] = arr;
    return res;
}


// Point the editor camera somewhere so an external screenshot shows
// the part of the map being discussed. Best effort: the orbital
// camera fields are not part of a documented API and have moved
// between game builds, so every write is guarded and the op reports
// what it managed rather than failing the whole command.
Json::Value MoveCamera(
    CGameCtnEditorFree@ editor, Json::Value@ body, Json::Value res
) {
    auto cam = editor.OrbitalCameraControl;
    if (cam is null) {
        res["ok"] = false;
        res["error"] = "no orbital camera control on this editor";
        return res;
    }
    Json::Value applied = Json::Array();
    try {
        if (body.HasKey("x") && body.HasKey("y") && body.HasKey("z")) {
            cam.m_TargetedPosition = vec3(
                float(body["x"]), float(body["y"]), float(body["z"]));
            applied.Add("position");
        }
        if (body.HasKey("distance")) {
            cam.m_TargetedDistance = float(body["distance"]);
            applied.Add("distance");
        }
        if (body.HasKey("h_angle")) {
            cam.m_CurrentHAngle = float(body["h_angle"]);
            applied.Add("h_angle");
        }
        if (body.HasKey("v_angle")) {
            cam.m_CurrentVAngle = float(body["v_angle"]);
            applied.Add("v_angle");
        }
        res["ok"] = true;
    } catch {
        res["ok"] = false;
        res["error"] = "camera write failed: " + getExceptionInfo();
    }
    res["applied"] = applied;
    return res;
}


string VStatusToString(CGameEditorPluginMapMapType::EValidationStatus st) {
    switch (st) {
        case CGameEditorPluginMapMapType::EValidationStatus::NotValidable:
            return "NotValidable";
        case CGameEditorPluginMapMapType::EValidationStatus::Validable:
            return "Validable";
        case CGameEditorPluginMapMapType::EValidationStatus::Validated:
            return "Validated";
    }
    return "Unknown";
}


void log(const string &in msg) {
    print("[TMMapControl] " + msg);
}
