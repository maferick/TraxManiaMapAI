// Enumerate a game class's members at RUNTIME, so nothing has to be
// guessed at compile time.
//
// Why this is its own plugin rather than an op on TMMapControl: in
// AngelScript an unknown member is a COMPILE-time error that takes the
// whole plugin down. Assuming CGameControlCameraEditorOrbital had
// m_TargetedDistance did exactly that earlier and cost a reload cycle.
// Reflection asks the engine for the member list instead of naming
// members in code, so this file cannot fail that way — and if the
// reflection surface itself is wrong, only this throwaway plugin dies.
//
// Answers two open questions before the telemetry collection run:
//   1. what respawn/reset accessor does CSmScriptPlayer actually expose
//   2. how does the checkpoint counter behave across laps
//
// Writes <PluginStorage>/APIProbe/members.json and logs a summary.

const string PLUGIN_VERSION = "api-probe-v0.1";

// Classes worth dumping. CSmScriptPlayer is the one AIReplayTelemetry
// already samples from.
const array<string> TYPES = {
    "CGameCtnEditorFree",
    "CGameEditorPluginMap",
    "CGameCtnEditorCommon",
    "CTrackMania",
    "CGameCtnChallenge"
};

// Substrings worth calling out in the log, so the answer is visible
// without reading 400 member names.
// Second question (2026-07-29): leaving the editor raises a modal
// "The map has been changed. Do you want to save your changes?", which
// swallows BackToMainMenu() and makes load_map return the PREVIOUS
// map. Looking for whatever marks the map clean, or quits without
// prompting, so the answer is given in-process instead of by clicking
// the screen.
const array<string> INTERESTING = {
    "Modif", "modif", "Dirty", "dirty", "Save", "save",
    "Quit", "quit", "Exit", "exit", "Close", "close",
    "Prompt", "prompt", "Dialog", "dialog", "Changed", "changed"
};


void Main() {
    log("version " + PLUGIN_VERSION);
    log("storage: " + IO::FromStorageFolder(""));
    Json::Value doc = Json::Object();
    doc["plugin_version"] = PLUGIN_VERSION;

    for (uint t = 0; t < TYPES.Length; t++) {
        string name = TYPES[t];
        auto ty = Reflection::GetType(name);
        if (ty is null) {
            log("MISSING TYPE: " + name);
            doc[name] = "missing";
            continue;
        }
        Json::Value members = Json::Array();
        for (uint i = 0; i < ty.Members.Length; i++) {
            auto m = ty.Members[i];
            if (m is null) continue;
            // MwMemberInfo exposes Name but NOT Type — found by the
            // compiler rejecting m.Type, which is the whole reason this
            // probe is a throwaway plugin.
            members.Add(m.Name);

            for (uint k = 0; k < INTERESTING.Length; k++) {
                if (m.Name.IndexOf(INTERESTING[k]) >= 0) {
                    log("  " + name + "." + m.Name);
                    break;
                }
            }
        }
        doc[name] = members;
        log(name + ": " + members.Length + " members");
    }

    Json::ToFile(IO::FromStorageFolder("members.json"), doc);
    log("wrote members.json");
}


void log(const string &in msg) {
    print("[APIProbe] " + msg);
}
