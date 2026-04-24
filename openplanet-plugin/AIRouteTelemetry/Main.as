// AI Route Telemetry — OpenPlanet plugin for the remote-test rig.
//
// v0.2: unattended finishability via CGameEditorPluginMap.Validate().
//
// TM2020's map editor exposes the same AI validator the game uses
// to gate TMX uploads. Opening a .Map.Gbx in the editor and calling
// Validate() runs that AI from Spawn to Goal and reports:
//   * ValidationStatus ∈ {NotValidable, Validable, Validated}
//   * Map.TMObjective_AuthorTime (set after a successful validate)
//
// "Validated" = the internal AI drove from spawn to goal cleanly.
// That's our "is this map finishable?" unattended signal — no
// OP input-simulation shenanigans, no human driver needed.
//
// Works with OpenPlanet 1.29.5 + AngelScript 2.39 WIP. Protocol
// stays ai_rig_v1; new fields (`validation_status`, `author_time_ms`)
// are additive and ignored by older agents.
//
// File conventions (unchanged from v0.1):
//   Input:   <PluginStorage>/AIRouteTelemetry/<job_id>.in.json
//   Output:  <PluginStorage>/AIRouteTelemetry/<job_id>.out.json
//   Plugin NEVER removes files it didn't author.

const string PLUGIN_VERSION = "plugin-v0.2";
const string PROTOCOL = "ai_rig_v1";

// Map must appear in the editor within this window after EditMap()
// is called. Covers first-launch editor warm-up on slower boxes.
const int EDITOR_OPEN_WAIT_SECONDS = 45;

// Validate() kicks off an in-game AI drive. For a short test map
// this completes in a few seconds; for a longer/complex map it
// can take 20–30s. 120s is a safety ceiling.
const int VALIDATE_WAIT_SECONDS = 120;

// Poll frequency for the trigger folder.
const int SCAN_INTERVAL_MS = 1000;


void Main() {
    string rigFolder = IO::FromStorageFolder("");
    log("rig folder: " + rigFolder);
    log("plugin version: " + PLUGIN_VERSION);

    while (true) {
        yield();
        array<string> pending = ScanForPending(rigFolder);
        for (uint i = 0; i < pending.Length; i++) {
            ProcessJob(pending[i]);
        }
        sleep(SCAN_INTERVAL_MS);
    }
}


array<string> ScanForPending(const string &in rigFolder) {
    array<string> out;
    array<string> entries = IO::IndexFolder(rigFolder, false);
    for (uint i = 0; i < entries.Length; i++) {
        string path = entries[i];
        if (!path.EndsWith(".in.json")) continue;
        string outPath = path.SubStr(0, path.Length - 8) + ".out.json";
        if (IO::FileExists(outPath)) continue;
        out.InsertLast(path);
    }
    return out;
}


// ---------------------------------------------------------------------
// Processing one job
// ---------------------------------------------------------------------

void ProcessJob(const string &in inPath) {
    log("processing " + inPath);

    Json::Value@ body = ReadJson(inPath);
    if (body is null || body.GetType() != Json::Type::Object) {
        log("skipping malformed .in.json: " + inPath);
        return;
    }
    if (string(body["protocol"]) != PROTOCOL) {
        log("protocol mismatch in " + inPath);
        return;
    }
    int jobId = int(body["job_id"]);
    string runId = string(body["run_id"]);
    string mapFile = string(body["map_file"]);
    int64 deadlineUnix = int64(body["deadline_unix"]);
    string outPath = inPath.SubStr(0, inPath.Length - 8) + ".out.json";

    bool loadSuccess = false;
    string loadError = "";
    string validationStatus = "Unknown";
    int authorTimeMs = -1;
    string exitReason = "other";

    auto app = cast<CTrackMania>(GetApp());
    if (app is null) {
        WriteOut(outPath, jobId, runId,
                 false, "GetApp() returned null — wrong game?",
                 "Unknown", -1, "load_error");
        return;
    }

    // Wait for title script API.
    while (!app.ManiaTitleControlScriptAPI.IsReady) {
        if (Time::Stamp >= deadlineUnix) {
            WriteOut(outPath, jobId, runId, false,
                     "title script API never ready", "Unknown", -1,
                     "load_error");
            return;
        }
        yield();
    }

    // If user is in a playground or editor already, back out first.
    // EditMap can't transition cleanly from every state.
    if (app.CurrentPlayground !is null || app.Editor !is null) {
        app.BackToMainMenu();
        while (!app.ManiaTitleControlScriptAPI.IsReady) {
            if (Time::Stamp >= deadlineUnix) {
                WriteOut(outPath, jobId, runId, false,
                         "back-to-menu hang", "Unknown", -1,
                         "load_error");
                return;
            }
            yield();
        }
    }

    // Open the map in the editor. Same TitleControlScriptAPI that
    // handles PlayMap; EditMap(url, updateMode, settingsXml).
    log("EditMap('" + mapFile + "')");
    app.ManiaTitleControlScriptAPI.EditMap(mapFile, "", "");

    // Wait for the editor + its PluginMapType to surface. If the
    // map has missing-resource / titlepack errors, the editor
    // surfaces them during open; we timeout + report load_error.
    int openDeadlineStamp = Time::Stamp + EDITOR_OPEN_WAIT_SECONDS;
    CGameEditorPluginMap@ pmt = null;
    while (Time::Stamp < openDeadlineStamp && Time::Stamp < deadlineUnix) {
        yield();
        auto editor = cast<CGameCtnEditorFree>(app.Editor);
        if (editor !is null && editor.PluginMapType !is null) {
            @pmt = editor.PluginMapType;
            if (pmt.IsEditorReadyForRequest) {
                loadSuccess = true;
                break;
            }
        }
        sleep(250);
    }

    if (!loadSuccess || pmt is null) {
        loadError = "editor did not open map within "
                    + EDITOR_OPEN_WAIT_SECONDS + "s "
                    + "(titlepack / missing resources / corrupt GBX)";
        WriteOut(outPath, jobId, runId, false, loadError,
                 "Unknown", -1, "load_error");
        app.BackToMainMenu();
        return;
    }

    // Kick off validation. The editor's AI driver attempts the map
    // from Spawn to Goal. ValidationStatus transitions:
    //   NotValidable → Validable → (game drives) → Validated
    // "NotValidable" means the map's topology itself is wrong
    // (missing Start/Finish, unlinked CPs). "Validated" = finish.
    log("Validate()");
    pmt.Validate();

    int validateDeadlineStamp = Time::Stamp + VALIDATE_WAIT_SECONDS;
    bool done = false;
    while (Time::Stamp < validateDeadlineStamp
           && Time::Stamp < deadlineUnix) {
        yield();
        validationStatus = VStatusToString(pmt.ValidationStatus);
        if (pmt.ValidationStatus
            == CGameEditorPluginMap::EValidationStatus::Validated) {
            done = true;
            break;
        }
        if (pmt.ValidationStatus
            == CGameEditorPluginMap::EValidationStatus::NotValidable) {
            // The map's structure is bad — no spawn, broken CPs, etc.
            // That's a definitive finishability-negative result; we
            // don't need to wait longer.
            done = true;
            break;
        }
        sleep(500);
    }

    // Pull the author time if the validator set it. CGameCtnChallenge
    // exposes TMObjective_AuthorTime after a successful validate.
    if (done && pmt.Map !is null) {
        authorTimeMs = int(pmt.Map.TMObjective_AuthorTime);
    }

    if (!done) {
        exitReason = "validation_timeout";
        validationStatus = VStatusToString(pmt.ValidationStatus);
    } else if (pmt.ValidationStatus
               == CGameEditorPluginMap::EValidationStatus::Validated) {
        exitReason = "validated";
    } else {
        exitReason = "not_validable";
    }

    app.BackToMainMenu();

    WriteOut(outPath, jobId, runId, loadSuccess, loadError,
             validationStatus, authorTimeMs, exitReason);
}


string VStatusToString(CGameEditorPluginMap::EValidationStatus s) {
    if (s == CGameEditorPluginMap::EValidationStatus::Validated)    return "Validated";
    if (s == CGameEditorPluginMap::EValidationStatus::Validable)    return "Validable";
    if (s == CGameEditorPluginMap::EValidationStatus::NotValidable) return "NotValidable";
    return "Unknown";
}


// ---------------------------------------------------------------------
// IO helpers
// ---------------------------------------------------------------------

Json::Value@ ReadJson(const string &in path) {
    IO::File f(path, IO::FileMode::Read);
    string content = f.ReadToEnd();
    f.Close();
    return Json::Parse(content);
}


void WriteOut(
    const string &in outPath,
    int jobId, const string &in runId,
    bool loadSuccess, const string &in loadError,
    const string &in validationStatus,
    int authorTimeMs,
    const string &in exitReason
) {
    Json::Value@ out = Json::Object();
    out["protocol"] = PROTOCOL;
    out["job_id"] = jobId;
    out["run_id"] = runId;
    out["load_success"] = loadSuccess;
    if (loadError.Length > 0) {
        out["load_error"] = loadError;
    }
    // v0.2 additive fields. Agent side treats them as optional so
    // older plugins stay compatible.
    out["validation_status"] = validationStatus;
    if (authorTimeMs >= 0) {
        out["author_time_ms"] = authorTimeMs;
    }
    // Derive the v0.1 compatibility fields so the agent's existing
    // aggregators keep working:
    bool finished = (validationStatus == "Validated");
    out["spawn_ok"] = loadSuccess;  // editor opening implies the
                                    // map has a valid spawn block
    out["finished"] = finished;
    out["checkpoint_times_ms"] = Json::Array();
    out["driven_cells"] = Json::Array();
    out["exit_reason"] = exitReason;
    out["plugin_version"] = PLUGIN_VERSION;

    Json::ToFile(outPath, out);
    log("wrote " + outPath + " (load=" + loadSuccess
        + " validation=" + validationStatus
        + " author_time_ms=" + authorTimeMs
        + " exit=" + exitReason + ")");
}


void log(const string &in msg) {
    trace("[AIRouteTelemetry] " + msg);
}
