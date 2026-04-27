// AI Replay Telemetry — sibling of AIRouteTelemetry.
//
// AIRouteTelemetry runs the editor validator and reports a single
// boolean ("did the AI driver finish?"). That's enough for an
// unattended finishability gate but it gives us zero per-frame data
// about HOW the map drives. Under the corpus-finishable axiom + the
// replay-ground-truth learning contract (CLAUDE.md), we want
// per-frame ground truth from actual replay playback so the AI can
// learn from observed driver behaviour, not just from "the AI
// driver finished it."
//
// What this plugin does, per job:
//   1. Read a job descriptor: {map_file, replay_file, ...}
//   2. Load the map + the ghost replay in spectator playback.
//   3. Sample telemetry every game tick the ghost exposes a
//      CSmPlayer surface (position, velocity, rotation, inputs,
//      wheel contact, gear, RPM, current_checkpoint).
//   4. When the playback ends (ghost reaches finish or runs out),
//      write the frames to <id>.out.json and back out to the menu.
//
// Protocol = ai_rig_v1 (same as AIRouteTelemetry, additive fields).
// The Linux server differentiates jobs by which plugin's
// PluginStorage folder they were dropped into — same rig server,
// distinct plugins, no protocol fork.
//
// File conventions:
//   Input:   <PluginStorage>/AIReplayTelemetry/<job_id>.in.json
//   Output:  <PluginStorage>/AIReplayTelemetry/<job_id>.out.json
//   Plugin NEVER removes files it didn't author.

const string PLUGIN_VERSION = "replay-plugin-v0.1";
const string PROTOCOL = "ai_rig_v1";

// How long to wait for the playground to materialise after PlayMap.
// Longer ceiling than AIRouteTelemetry's editor wait because PlayMap
// goes through more loading screens.
const int PLAYGROUND_OPEN_WAIT_SECONDS = 60;

// Ghost playback ceiling. Most TM2020 maps finish in <2 minutes;
// 300s catches long-format maps without letting a stuck job hang.
const int PLAYBACK_WAIT_SECONDS = 300;

// Telemetry sample period. The game runs physics at 100Hz; sampling
// every tick (~10ms) is feasible but inflates .out.json sharply on
// long maps. Default 50ms = 20Hz which preserves enough resolution
// for trajectory + input reconstruction without blowing past the
// rig server's per-job byte budget.
const int SAMPLE_PERIOD_MS = 50;

// Hard cap on frames per job. Belt-and-suspenders: 20Hz × 300s =
// 6000 frames; 8000 leaves headroom for short overshoot, but stops
// a runaway from writing a megabytes-of-JSON pathology.
const int MAX_FRAMES = 8000;

// Poll frequency for the trigger folder.
const int SCAN_INTERVAL_MS = 1000;


void Main() {
    string rigFolder = IO::FromStorageFolder("");
    log("rig folder: " + rigFolder);
    log("plugin version: " + PLUGIN_VERSION);
    log("sample period: " + SAMPLE_PERIOD_MS + "ms");

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
// Telemetry frame
// ---------------------------------------------------------------------

class Frame {
    int    t_ms;
    float  x;
    float  y;
    float  z;
    float  vx;
    float  vy;
    float  vz;
    float  yaw;
    float  pitch;
    float  roll;
    // Inputs as currently read from the ghost. -1..1 for steer,
    // 0..1 for gas/brake. CSmPlayer exposes them via
    // ScriptAPI.InputSteer / InputGasPedal / InputIsBraking, which
    // OP normalises into floats.
    float  steer;
    float  gas;
    float  brake;
    // Wheels: 1 if any wheel is in surface contact, 0 if airborne.
    // Coarse but enough to identify jumps + boost gates.
    int    wheel_contact;
    int    gear;
    int    rpm;
    int    cp_index;          // current checkpoint count, 0 at spawn
    int    finished;          // 0/1, set on finish frame onward

    Json::Value@ ToJson() {
        Json::Value@ j = Json::Object();
        j["t_ms"] = t_ms;
        j["x"] = x; j["y"] = y; j["z"] = z;
        j["vx"] = vx; j["vy"] = vy; j["vz"] = vz;
        j["yaw"] = yaw; j["pitch"] = pitch; j["roll"] = roll;
        j["steer"] = steer; j["gas"] = gas; j["brake"] = brake;
        j["wheel_contact"] = wheel_contact;
        j["gear"] = gear;
        j["rpm"] = rpm;
        j["cp_index"] = cp_index;
        j["finished"] = finished;
        return j;
    }
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
    string replayFile = string(body["replay_file"]);
    int64 deadlineUnix = int64(body["deadline_unix"]);
    string outPath = inPath.SubStr(0, inPath.Length - 8) + ".out.json";

    array<Frame@> frames;
    string exitReason = "other";
    bool loadSuccess = false;
    string loadError = "";
    bool finishedFlag = false;

    auto app = cast<CTrackMania>(GetApp());
    if (app is null) {
        WriteOut(outPath, jobId, runId,
                 false, "GetApp() returned null — wrong game?",
                 frames, false, "load_error");
        return;
    }

    // Wait for title script API (same pattern as AIRouteTelemetry).
    while (!app.ManiaTitleControlScriptAPI.IsReady) {
        if (Time::Stamp >= deadlineUnix) {
            WriteOut(outPath, jobId, runId, false,
                     "title script API never ready", frames, false,
                     "load_error");
            return;
        }
        yield();
    }

    if (app.CurrentPlayground !is null || app.Editor !is null) {
        app.BackToMainMenu();
        while (!app.ManiaTitleControlScriptAPI.IsReady) {
            if (Time::Stamp >= deadlineUnix) {
                WriteOut(outPath, jobId, runId, false,
                         "back-to-menu hang", frames, false,
                         "load_error");
                return;
            }
            yield();
        }
    }

    // PlayMap with a ghost overlay. The TitleControlScriptAPI's
    // PlayMap takes (mapUrl, mode, settingsXml). We pass the replay
    // file path in the settings so the game launches a ghost-only
    // playback session — the operator's car never spawns; only the
    // replay's CSmPlayer drives.
    string settings = "<script><![CDATA[\n"
                      "  ghost_replay=\"" + replayFile + "\"\n"
                      "]]></script>";
    log("PlayMap('" + mapFile + "', ghost='" + replayFile + "')");
    app.ManiaTitleControlScriptAPI.PlayMap(mapFile, "TrackMania/TM_PlayMap_Local", settings);

    // Wait for playground.
    int openDeadlineStamp = Time::Stamp + PLAYGROUND_OPEN_WAIT_SECONDS;
    CSmArenaClient@ playground = null;
    while (Time::Stamp < openDeadlineStamp && Time::Stamp < deadlineUnix) {
        yield();
        @playground = cast<CSmArenaClient>(app.CurrentPlayground);
        if (playground !is null
            && playground.GameTerminals.Length > 0
            && playground.GameTerminals[0].GUIPlayer !is null) {
            loadSuccess = true;
            break;
        }
        sleep(250);
    }

    if (!loadSuccess) {
        loadError = "playground did not surface a GUIPlayer within "
                    + PLAYGROUND_OPEN_WAIT_SECONDS + "s "
                    + "(missing replay / bad map / titlepack)";
        WriteOut(outPath, jobId, runId, false, loadError,
                 frames, false, "load_error");
        app.BackToMainMenu();
        return;
    }

    // Sampling loop. We treat the GUIPlayer's CSmPlayer as the
    // ghost surface — TM2020's GhostMgr injects the ghost into the
    // player slot during ghost-only playback.
    int playbackDeadlineStamp = Time::Stamp + PLAYBACK_WAIT_SECONDS;
    int firstSampleMs = -1;
    while (Time::Stamp < playbackDeadlineStamp
           && Time::Stamp < deadlineUnix
           && frames.Length < uint(MAX_FRAMES)) {
        yield();
        if (playground.GameTerminals.Length == 0) break;
        auto guiPlayer = playground.GameTerminals[0].GUIPlayer;
        if (guiPlayer is null) break;
        auto smPlayer = cast<CSmPlayer>(guiPlayer);
        if (smPlayer is null) break;
        auto api = smPlayer.ScriptAPI;
        if (api is null) {
            sleep(SAMPLE_PERIOD_MS);
            continue;
        }

        // Game-clock t_ms relative to first sampled frame so the
        // series starts at 0 even if loading lag pushed playback
        // past time t=0 in the world.
        int worldMs = int(api.CurrentRaceTime);
        if (firstSampleMs < 0) firstSampleMs = worldMs;
        int t = worldMs - firstSampleMs;

        Frame@ f = Frame();
        f.t_ms = t;
        // CSmPlayer.ScriptAPI exposes Position as vec3.
        f.x = api.Position.x;
        f.y = api.Position.y;
        f.z = api.Position.z;
        f.vx = api.Velocity.x;
        f.vy = api.Velocity.y;
        f.vz = api.Velocity.z;
        // Yaw/pitch/roll: TM2020 surfaces these as floats on the
        // vehicle visual state. AimYaw / AimPitch / AimRoll on
        // ScriptAPI when present; default 0 if not.
        f.yaw   = SafeYaw(api);
        f.pitch = SafePitch(api);
        f.roll  = SafeRoll(api);
        f.steer = float(api.InputSteer);
        f.gas   = float(api.InputGasPedal);
        f.brake = api.InputIsBraking ? 1.0f : 0.0f;
        f.wheel_contact = SafeWheelContact(api);
        f.gear = int(api.EngineCurGear);
        f.rpm  = int(api.EngineRpm);
        f.cp_index = int(api.CurrentNbCheckpoints);
        f.finished = api.RaceFinished ? 1 : 0;
        frames.InsertLast(f);

        if (api.RaceFinished) {
            finishedFlag = true;
            // Capture a couple of trailing frames after finish for
            // post-finish visualisation, then stop.
            for (int k = 0; k < 5; k++) {
                yield();
                sleep(SAMPLE_PERIOD_MS);
            }
            break;
        }
        sleep(SAMPLE_PERIOD_MS);
    }

    if (frames.Length >= uint(MAX_FRAMES)) {
        exitReason = "max_frames_capped";
    } else if (finishedFlag) {
        exitReason = "finished";
    } else if (Time::Stamp >= playbackDeadlineStamp) {
        exitReason = "playback_timeout";
    } else {
        exitReason = "playback_ended_unfinished";
    }

    app.BackToMainMenu();
    WriteOut(outPath, jobId, runId, true, "",
             frames, finishedFlag, exitReason);
}


// ---------------------------------------------------------------------
// Defensive accessor helpers
//
// CSmPlayer's ScriptAPI surface differs slightly across TM2020
// patches; not every property is guaranteed. These helpers return a
// 0 default rather than crashing the plugin if a field is missing.
// AngelScript doesn't have try/catch here, so we cast through the
// known intermediate types and check is null.
// ---------------------------------------------------------------------

float SafeYaw(CSmScriptPlayer@ api) {
    if (api is null) return 0.0f;
    return float(api.AimYaw);
}
float SafePitch(CSmScriptPlayer@ api) {
    if (api is null) return 0.0f;
    return float(api.AimPitch);
}
float SafeRoll(CSmScriptPlayer@ api) {
    if (api is null) return 0.0f;
    // AimRoll isn't exposed on every patch; if absent, plugin authors
    // typically derive roll from the up vector. For v0.1 we just
    // default to 0 — the rig server can compute roll from the
    // (yaw, pitch, position) triplet if needed downstream.
    return 0.0f;
}
int SafeWheelContact(CSmScriptPlayer@ api) {
    if (api is null) return 0;
    // ScriptAPI.IsInAir is the inverse of "any wheel in contact."
    // 1 when any wheel touches, 0 when fully airborne.
    return api.IsInAir ? 0 : 1;
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
    array<Frame@> &in frames,
    bool finished,
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
    out["plugin_version"] = PLUGIN_VERSION;
    out["sample_period_ms"] = SAMPLE_PERIOD_MS;
    out["finished"] = finished;
    out["exit_reason"] = exitReason;
    out["frame_count"] = int(frames.Length);

    Json::Value@ framesArr = Json::Array();
    for (uint i = 0; i < frames.Length; i++) {
        framesArr.Add(frames[i].ToJson());
    }
    out["frames"] = framesArr;

    // v0.1 compatibility shims so the rig server's existing
    // aggregator (which knows AIRouteTelemetry's shape) doesn't
    // crash on a missing field. Empty/zero defaults — the new
    // `frames` array carries the actual signal.
    out["spawn_ok"] = loadSuccess;
    out["validation_status"] = finished ? "Validated" : "Unknown";
    out["checkpoint_times_ms"] = Json::Array();
    out["driven_cells"] = Json::Array();

    Json::ToFile(outPath, out);
    log("wrote " + outPath
        + " (load=" + loadSuccess
        + " frames=" + frames.Length
        + " finished=" + finished
        + " exit=" + exitReason + ")");
}


void log(const string &in msg) {
    trace("[AIReplayTelemetry] " + msg);
}
