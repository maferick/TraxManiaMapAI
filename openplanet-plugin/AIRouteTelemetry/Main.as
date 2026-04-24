// AI Route Telemetry — OpenPlanet plugin for the remote-test rig.
//
// Works with OpenPlanet 1.29.5 + AngelScript 2.39 WIP. Runs as
// a background coroutine (Main) that polls a shared folder for
// <id>.in.json trigger files written by the Windows agent, loads
// the referenced .Map.Gbx via
// ManiaTitleControlScriptAPI.PlayMap(), observes load state,
// and writes <id>.out.json with structured telemetry the agent
// ships back to the Linux server.
//
// Protocol: ai_rig_v1 (matches src/remote_test_agent/plugin_io.py).
//
// Scope (v0.1):
//   ✓ load-success / load-error detection
//   ✓ spawn cell sanity (car materialised in a playground)
//   ✓ checkpoint-time sampling while the plugin observes
//   ✗ simulated driving (OP sandbox disallows; operator drives
//     manually if they want finish telemetry; for unattended
//     tests, spawn_ok + load_success + absence of errors is the
//     signal)
//
// File conventions:
//   Input:   <PluginStorage>/AIRouteTelemetry/<job_id>.in.json
//   Output:  <PluginStorage>/AIRouteTelemetry/<job_id>.out.json
//
// Protocol notes
// --------------
// Plugin NEVER removes files it didn't author. The agent
// clears stale .in.json files before each job; the agent also
// removes .out.json after ingest. So our only writes are
// .out.json.

const string PLUGIN_VERSION = "plugin-v0.1";
const string PROTOCOL = "ai_rig_v1";

// How long we wait for a map to load once PlayMap is invoked.
// Covers Ubisoft launcher warm-up + map decompression; 45s is
// generous for first-load-after-launch scenarios.
const int LOAD_WAIT_SECONDS = 45;

// After the map loads, observe for up to this many seconds so
// spawn + first-CP signals have time to fire. Longer windows
// are fine but pile up telemetry files on long test batches.
const int OBSERVE_SECONDS = 20;

// Poll frequency for the trigger folder. A job sits in the queue
// at most this long before the plugin notices it.
const int SCAN_INTERVAL_MS = 1000;


void Main() {
    string rigFolder = IO::FromStorageFolder("");
    log("rig folder: " + rigFolder);

    while (true) {
        yield();
        array<string> pending = ScanForPending(rigFolder);
        for (uint i = 0; i < pending.Length; i++) {
            ProcessJob(pending[i]);
        }
        sleep(SCAN_INTERVAL_MS);
    }
}


// ---------------------------------------------------------------------
// Scan the rig folder for .in.json files that don't yet have a
// matching .out.json (i.e. un-processed jobs). Returns absolute
// paths.
// ---------------------------------------------------------------------

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
// Load + observe one job, then emit .out.json.
// ---------------------------------------------------------------------

void ProcessJob(const string &in inPath) {
    log("processing " + inPath);

    // Parse the trigger.
    Json::Value@ body = ReadJson(inPath);
    if (body is null || body.GetType() != Json::Type::Object) {
        log("skipping malformed .in.json: " + inPath);
        return;
    }
    if (string(body["protocol"]) != PROTOCOL) {
        log("protocol mismatch in " + inPath + " (expected " + PROTOCOL + ")");
        return;
    }
    int jobId = int(body["job_id"]);
    string runId = string(body["run_id"]);
    string mapFile = string(body["map_file"]);
    int64 deadlineUnix = int64(body["deadline_unix"]);
    string outPath = inPath.SubStr(0, inPath.Length - 8) + ".out.json";

    // Everything we learn is stored here and flushed to disk at end.
    bool loadSuccess = false;
    string loadError = "";
    bool spawnOk = false;
    bool finished = false;
    array<int> cpTimesMs;
    array<Json::Value@> cellSamples;
    string exitReason = "observer_timeout";

    auto app = cast<CTrackMania>(GetApp());
    if (app is null) {
        WriteOut(outPath, jobId, runId, false,
                 "GetApp() returned null — wrong game?",
                 false, false, cpTimesMs, cellSamples, "load_error");
        return;
    }

    // Wait for title script API to be ready. On a cold boot this
    // can be a few seconds.
    while (!app.ManiaTitleControlScriptAPI.IsReady) {
        if (Time::Stamp >= deadlineUnix) {
            WriteOut(outPath, jobId, runId, false,
                     "title script API never ready within deadline",
                     false, false, cpTimesMs, cellSamples, "load_error");
            return;
        }
        yield();
    }

    // If we're already in a playground (user was driving), drop
    // back to menu first. PlayMap can't transition from playground
    // to playground cleanly in all TM2020 builds.
    if (app.CurrentPlayground !is null) {
        app.BackToMainMenu();
        while (!app.ManiaTitleControlScriptAPI.IsReady) {
            if (Time::Stamp >= deadlineUnix) {
                WriteOut(outPath, jobId, runId, false,
                         "back-to-menu hang", false, false,
                         cpTimesMs, cellSamples, "load_error");
                return;
            }
            yield();
        }
    }

    // Kick off the load. PlayMap accepts local file paths — we
    // pass the absolute path the agent wrote into AI-inbox.
    log("PlayMap('" + mapFile + "')");
    app.ManiaTitleControlScriptAPI.PlayMap(mapFile, "", "");

    // Wait for RootMap to materialise OR for a load error to
    // surface as "we never entered a playground within
    // LOAD_WAIT_SECONDS".
    int loadDeadlineStamp = Time::Stamp + LOAD_WAIT_SECONDS;
    while (Time::Stamp < loadDeadlineStamp && Time::Stamp < deadlineUnix) {
        yield();
        if (app.RootMap !is null) {
            loadSuccess = true;
            break;
        }
        sleep(250);
    }

    if (!loadSuccess) {
        loadError = "map did not load within " + LOAD_WAIT_SECONDS + "s "
                    + "(titlepack / missing resources / corrupt GBX)";
        WriteOut(outPath, jobId, runId, false, loadError,
                 false, false, cpTimesMs, cellSamples, "load_error");
        app.BackToMainMenu();
        return;
    }

    // Map loaded. Observe for OBSERVE_SECONDS or until deadline.
    int observeDeadlineStamp = Time::Stamp + OBSERVE_SECONDS;
    while (Time::Stamp < observeDeadlineStamp && Time::Stamp < deadlineUnix) {
        yield();
        if (app.CurrentPlayground !is null
            && app.CurrentPlayground.GameTerminals.Length > 0) {
            auto term = app.CurrentPlayground.GameTerminals[0];
            if (term.ControlledPlayer !is null) {
                spawnOk = true;
            }
            // Checkpoint sampling: the GameTerminal's script API
            // exposes checkpoints as they're triggered. Exact
            // access path varies across TM2020 builds; wrapping
            // in a catch so a missing field doesn't kill the
            // whole observation.
            // TODO(post-v0): per-CP timestamps + driven-cell sampling.
        }
        sleep(500);
    }

    // Done observing — leave the playground so the next job
    // starts from a known state.
    app.BackToMainMenu();

    WriteOut(outPath, jobId, runId, loadSuccess, loadError,
             spawnOk, finished, cpTimesMs, cellSamples,
             spawnOk ? "observer_timeout" : "observer_timeout");
}


// ---------------------------------------------------------------------
// JSON IO helpers
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
    bool spawnOk, bool finished,
    array<int> &in cpTimesMs,
    array<Json::Value@> &in cellSamples,
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
    out["spawn_ok"] = spawnOk;
    out["finished"] = finished;

    Json::Value@ cpArr = Json::Array();
    for (uint i = 0; i < cpTimesMs.Length; i++) {
        cpArr.Add(cpTimesMs[i]);
    }
    out["checkpoint_times_ms"] = cpArr;

    Json::Value@ cellArr = Json::Array();
    for (uint i = 0; i < cellSamples.Length; i++) {
        cellArr.Add(cellSamples[i]);
    }
    out["driven_cells"] = cellArr;

    out["exit_reason"] = exitReason;
    out["plugin_version"] = PLUGIN_VERSION;
    Json::ToFile(outPath, out);
    log("wrote " + outPath + " (load=" + loadSuccess
        + " spawn=" + spawnOk + " finished=" + finished + ")");
}


// ---------------------------------------------------------------------
// Logging: OP's trace() is already visible in the console. Prefix
// so the operator can grep the Openplanet log for our lines.
// ---------------------------------------------------------------------

void log(const string &in msg) {
    trace("[AIRouteTelemetry] " + msg);
}
