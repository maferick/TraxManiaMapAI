// Reload another plugin on command, so iterating on a plugin does not
// need a human clicking Developer > Reload every time.
//
// This session spent roughly six reload cycles on compile errors in
// TMMapControl and AIReplayTelemetry. Each one needed the operator.
// Automating it turns a two-minute round trip into a two-second one.
//
// UNVERIFIED SURFACE. Openplanet's `Meta` namespace is not in
// Openplanet.h — that dump covers GAME classes, not the scripting
// API — so unlike every other accessor used in this repo, these calls
// could not be checked before writing them. That is exactly why this
// is its own plugin: if Meta::GetPluginFromID or Plugin::Reload do not
// exist, only this file fails to compile and everything else keeps
// running. If it does fail, read the error and fix the names; the
// bootstrap reload is the one that always has to be manual.
//
// Protocol, matching the other plugins:
//   <PluginStorage>/PluginReloader/<id>.cmd.json   {"plugin":"TMMapControl"}
//   <PluginStorage>/PluginReloader/<id>.res.json

const string PLUGIN_VERSION = "plugin-reloader-v0.1";
const int SCAN_INTERVAL_MS = 400;


void Main() {
    log("version " + PLUGIN_VERSION);
    log("drop {\"plugin\":\"<id>\"} in " + IO::FromStorageFolder(""));
    while (true) {
        yield();
        array<string> entries = IO::IndexFolder(IO::FromStorageFolder(""), false);
        for (uint i = 0; i < entries.Length; i++) {
            string path = entries[i];
            if (!path.EndsWith(".cmd.json")) continue;
            string resPath = path.SubStr(0, path.Length - 9) + ".res.json";
            if (IO::FileExists(resPath)) continue;
            Handle(path, resPath);
        }
        sleep(SCAN_INTERVAL_MS);
    }
}


void Handle(const string &in cmdPath, const string &in resPath) {
    Json::Value res = Json::Object();
    res["plugin_version"] = PLUGIN_VERSION;

    Json::Value@ body = Json::FromFile(cmdPath);
    if (body is null || body.GetType() != Json::Type::Object) {
        res["ok"] = false;
        res["error"] = "malformed command json";
        Json::ToFile(resPath, res);
        return;
    }
    string target = string(body["plugin"]);
    res["plugin"] = target;

    // Never reload ourselves — that would tear down the coroutine
    // mid-write and leave the caller waiting on a response file that
    // never appears.
    if (target == "PluginReloader") {
        res["ok"] = false;
        res["error"] = "refusing to reload self";
        Json::ToFile(resPath, res);
        return;
    }

    auto plugin = Meta::GetPluginFromID(target);
    if (plugin is null) {
        res["ok"] = false;
        res["error"] = "no plugin with id '" + target + "'";
        Json::ToFile(resPath, res);
        return;
    }

    // Write the response BEFORE reloading. A reload can stall or take
    // the target down, and a caller blocked on the file should learn
    // that the request was accepted either way.
    res["ok"] = true;
    res["reloading"] = plugin.Name;
    Json::ToFile(resPath, res);
    log("reloading " + target);
    Meta::ReloadPlugin(target);
}


void log(const string &in msg) {
    print("[PluginReloader] " + msg);
}
