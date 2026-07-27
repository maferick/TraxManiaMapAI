// Reload another plugin on command, so iterating on a plugin does not
// need a human clicking Developer > Reload every time.
//
// This session spent roughly six reload cycles on compile errors in
// TMMapControl and AIReplayTelemetry. Each one needed the operator.
// Automating it turns a two-minute round trip into a two-second one.
//
// The `Meta` namespace is NOT in Openplanet.h — that dump covers game
// classes, not the scripting API — so unlike every other accessor here
// these calls could not be grepped first. Isolating them in their own
// plugin was the mitigation, and it worked: the first version passed
// a string to Meta::ReloadPlugin, which wants a Meta::Plugin@, and
// nothing else was affected. AngelScript prints the correct signature
// on an overload mismatch, so read the log rather than guessing again.
//
// The bootstrap load is always manual, and a NEW plugin folder may
// need Openplanet to rescan rather than just reload.
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
    // Takes the handle, not the id — the compiler prints the signature
    // on a mismatch, which is the fastest way to get these right.
    Meta::ReloadPlugin(plugin);
}


void log(const string &in msg) {
    print("[PluginReloader] " + msg);
}
