// AI Replay Telemetry. Per-frame ghost trajectory capture.
//
// AIRouteTelemetry runs the editor validator and reports a single
// boolean ("did the AI driver finish?"). That is enough for an
// unattended finishability gate but it gives us zero per-frame data
// about HOW the map drives. Under the corpus-finishable axiom plus the
// replay-ground-truth learning contract (CLAUDE.md), we want per-frame
// ground truth from actual replay playback so the AI can learn from
// observed driver behaviour.
//
// What this plugin does, per job:
//   1. Read a job descriptor: {map_file, replay_file, ...}
//   2. PlayMap the map, then load the replay's ghost and add it to
//      the playground as a ghost layer.
//   3. Sample the GHOST's position every SAMPLE_PERIOD_MS until the
//      ghost's replay is over.
//   4. Write the frames plus the ghost's own race result to
//      <id>.out.json.
//
// Protocol = ai_rig_v1 (same as AIRouteTelemetry, additive fields).
//
// File conventions:
//   Input:   <PluginStorage>/AIReplayTelemetry/<job_id>.in.json
//   Output:  <PluginStorage>/AIReplayTelemetry/<job_id>.out.json
//   Plugin NEVER removes files it didn't author.
//
// ---------------------------------------------------------------------
// v0.2, the rewrite that made this actually work.
//
// v0.1 had two independent bugs, either of which alone produced the
// observed "4,552 frames, t_ms always 0, car never moves":
//
//   1. GHOST NEVER LOADED. It called
//        PlayMap(map, mode, "<script>ghost_replay=...</script>")
//      That settings XML is INVENTED. No such key exists, and there is
//      no way to attach a ghost through PlayMap's settings string.
//
//   2. IT SAMPLED THE WRONG CAR. Even with a ghost loaded, the loop
//      read GameTerminals[0].GUIPlayer, which is the OPERATOR's own
//      car. That car sits at spawn untouched for the whole job. Fixing
//      (1) without fixing (2) still records a stationary vehicle.
//
// The working mechanism is CSmArenaRulesMode, reached as
// cast<CSmArenaRulesMode>(app.PlaygroundScript). One object carries the
// entire job (Openplanet.h line refs):
//
//     DataFileMgr            inherited from CGamePlaygroundScript  5934
//     Replay_Load(path)      -> CWebServicesTaskResult_GhostListScript
//                                                                 10910
//     Ghost_Add(ghost, bool) -> MwId                              23088
//     Ghost_IsReplayOver(id) -> bool                              23091
//     Ghost_GetPosition(id)  -> vec3                              23093
//     Now                    game clock, ms                       5893
//
// NOTE for future maintainers: CGameGhostMgrScript (Openplanet.h:12226)
// ALSO has Ghost_Add, and it is the obvious-looking choice. Do not use
// it. It has NO Ghost_GetPosition, so a ghost added through it cannot
// be sampled at all. That dead end already cost this project a cycle.
//
// LIMIT: Ghost_GetPosition yields position only. Velocity is
// differentiated downstream by the Python adapter. Steering, throttle,
// brake, gear, RPM and wheel contact are NOT obtainable for a ghost
// through any typed API. The rich struct (CSceneVehicleVisState,
// Openplanet.h:18454) has no typed accessor anywhere in the dump and is
// reachable only via memory-offset hacks. Route inference consumes
// trajectory, so position is sufficient. Do not add fake zero-valued
// input columns to make the schema look fuller.
// ---------------------------------------------------------------------

const string PLUGIN_VERSION = "replay-plugin-v0.2";
const string PROTOCOL = "ai_rig_v1";

// How long to wait for the playground to materialise after PlayMap.
const int PLAYGROUND_OPEN_WAIT_SECONDS = 60;

// How long to wait for Replay_Load's async task to settle.
const int REPLAY_LOAD_WAIT_SECONDS = 30;

// Ghost playback ceiling.
const int PLAYBACK_WAIT_SECONDS = 300;

// Telemetry sample period. 50ms = 20Hz, enough to reconstruct a
// trajectory without blowing the per-job byte budget.
const int SAMPLE_PERIOD_MS = 50;

// Hard cap on frames per job.
const int MAX_FRAMES = 8000;

// Ghost_IsReplayOver reads true before playback begins, so honouring it
// immediately would end every job at zero frames. It is gated on the
// ghost having actually entered the scene.
//
// How long to wait for that. Measured: a big map can take 16.7s between
// the playground existing and the ghost appearing, because sampling
// starts as soon as the playground and rules mode exist, which is well
// before the map finishes loading and the race starts. An earlier build
// used an 8s cap and lost 5 of 20 pilot maps to it.
const int GHOST_SPAWN_WAIT_MS = 60000;

// Ghost_GetPosition returns exactly (0,0,0) until the ghost is placed in
// the scene. Map coordinates put the playable area well away from the
// origin (ground row is y=9, i.e. 8m absolute), so a car reading as the
// origin is a null, never a real pose.
const float ORIGIN_EPSILON_M = 0.01f;

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
    array<string> pending;
    array<string> entries = IO::IndexFolder(rigFolder, false);
    for (uint i = 0; i < entries.Length; i++) {
        string path = entries[i];
        if (!path.EndsWith(".in.json")) continue;
        string donePath = path.SubStr(0, path.Length - 8) + ".out.json";
        if (IO::FileExists(donePath)) continue;
        pending.InsertLast(path);
    }
    return pending;
}


// ---------------------------------------------------------------------
// Telemetry frame
//
// Position plus clock only. See the LIMIT note in the header before
// adding fields here. Anything beyond this is not readable from a ghost.
// ---------------------------------------------------------------------

class Frame {
    int   t_ms;
    float x;
    float y;
    float z;

    Json::Value@ ToJson() {
        Json::Value@ j = Json::Object();
        j["t_ms"] = t_ms;
        j["x"] = x;
        j["y"] = y;
        j["z"] = z;
        return j;
    }
}


// ---------------------------------------------------------------------
// Ghost race result
//
// The ghost carries its own authoritative result: finish time, respawn
// count and per-checkpoint split times. For the 763 linked-checkpoint
// gold-set maps this is free ground truth to check route inference
// against. The checkpoint count and split times must agree with
// whatever block sequence we infer from the trajectory.
//
// MAINTENANCE: if this plugin ever stops compiling after a game update,
// suspect this function first. Ghost.Nickname and Result.Time are proven
// in a published plugin; NbRespawns and Checkpoints are plainly typed in
// Openplanet.h (CTmRaceResultNod, line 8972) but less travelled.
// ---------------------------------------------------------------------

Json::Value@ GhostResultToJson(CGameGhostScript@ ghost) {
    Json::Value@ j = Json::Object();
    if (ghost is null) return j;
    j["nickname"] = ghost.Nickname;
    auto result = ghost.Result;
    if (result is null) return j;
    j["time_ms"] = int(result.Time);
    j["nb_respawns"] = int(result.NbRespawns);
    Json::Value@ splits = Json::Array();
    for (uint i = 0; i < result.Checkpoints.Length; i++) {
        splits.Add(int(result.Checkpoints[i]));
    }
    j["checkpoint_times_ms"] = splits;
    return j;
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
    // Optional. Absent or empty means "use the map's own author ghost",
    // which is the normal case for corpus work.
    string replayFile = body.HasKey("replay_file")
        ? string(body["replay_file"]) : "";
    int64 deadlineUnix = int64(body["deadline_unix"]);
    string donePath = inPath.SubStr(0, inPath.Length - 8) + ".out.json";

    array<Frame@> frames;
    Json::Value@ ghostResult = Json::Object();

    auto app = cast<CTrackMania>(GetApp());
    if (app is null) {
        WriteOut(donePath, jobId, runId, false,
                 "GetApp() returned null, wrong game?",
                 frames, ghostResult, false, "load_error");
        return;
    }

    // Deliberately a warning, not a hard failure. IO::FileExists and
    // Replay_Load do not necessarily resolve paths the same way: PlayMap
    // wants a game-relative path ("Maps/My Maps/x.Map.Gbx") while
    // IO::FileExists works on real filesystem paths. If the two
    // disagree, let Replay_Load render the verdict; it reports a usable
    // ErrorType/ErrorCode that a pre-emptive bail would hide.
    if (replayFile.Length > 0 && !IO::FileExists(replayFile)) {
        log("WARN IO::FileExists says no such file: " + replayFile
            + " (trying Replay_Load anyway)");
    }

    // Wait for title script API (same pattern as AIRouteTelemetry).
    while (!app.ManiaTitleControlScriptAPI.IsReady) {
        if (Time::Stamp >= deadlineUnix) {
            WriteOut(donePath, jobId, runId, false,
                     "title script API never ready",
                     frames, ghostResult, false, "load_error");
            return;
        }
        yield();
    }

    if (app.CurrentPlayground !is null || app.Editor !is null) {
        app.BackToMainMenu();
        while (!app.ManiaTitleControlScriptAPI.IsReady) {
            if (Time::Stamp >= deadlineUnix) {
                WriteOut(donePath, jobId, runId, false,
                         "back-to-menu hang",
                         frames, ghostResult, false, "load_error");
                return;
            }
            yield();
        }
    }

    // Plain PlayMap. The ghost is attached AFTER the playground exists,
    // via Ghost_Add. There is no settings-XML route for it.
    log("PlayMap('" + mapFile + "')");
    app.ManiaTitleControlScriptAPI.PlayMap(
        mapFile, "TrackMania/TM_PlayMap_Local", "");

    // Wait for both the playground AND the rules-mode script. The rules
    // mode owns Ghost_Add and appears slightly after the playground
    // does, so waiting on the playground alone races.
    int openDeadline = Time::Stamp + PLAYGROUND_OPEN_WAIT_SECONDS;
    CSmArenaClient@ playground = null;
    CSmArenaRulesMode@ pgs = null;
    while (Time::Stamp < openDeadline && Time::Stamp < deadlineUnix) {
        yield();
        @playground = cast<CSmArenaClient>(app.CurrentPlayground);
        @pgs = cast<CSmArenaRulesMode>(app.PlaygroundScript);
        if (playground !is null && pgs !is null) break;
        sleep(250);
    }

    if (playground is null || pgs is null) {
        WriteOut(donePath, jobId, runId, false,
                 "playground/rules-mode did not surface within "
                 + PLAYGROUND_OPEN_WAIT_SECONDS + "s "
                 + "(bad map path or titlepack?)",
                 frames, ghostResult, false, "load_error");
        app.BackToMainMenu();
        return;
    }

    auto dataFileMgr = pgs.DataFileMgr;
    if (dataFileMgr is null) {
        WriteOut(donePath, jobId, runId, false, "DataFileMgr is null",
                 frames, ghostResult, false, "load_error");
        app.BackToMainMenu();
        return;
    }

    // Two ways to obtain a ghost.
    //
    // DEFAULT (job omits replay_file): Map_GetAuthorGhost pulls the
    // author's validation ghost straight out of the loaded map
    // (Openplanet.h:10874). A map that has one carries it internally,
    // so this needs no .Replay.Gbx on disk and no extraction step at
    // all. Recipe confirmed from skybaks' ExtractValidationReplay.
    //
    // EXPLICIT replay_file: Replay_Load, for driving a ghost that is
    // NOT the author's, e.g. a leaderboard replay or one of our own.
    //
    // DEAD END, do not retry: the embedded ghost is also visible as
    // ChallengeParameters.RaceValidateGhost (Openplanet.h:1935), which
    // is the obvious-looking route. It is a CGameCtnGhost, while
    // Ghost_Add takes a CGameGhostScript. Those are unrelated siblings
    // off CMwNod and NOTHING in the API converts between them, so
    // RaceValidateGhost cannot feed playback. Map_GetAuthorGhost can,
    // because it hands back the script type.
    CGameGhostScript@ ghost = null;
    CWebServicesTaskResult_GhostListScript@ task = null;
    string ghostSource = "";

    if (replayFile.Length > 0) {
        ghostSource = "replay_file";
        log("Replay_Load('" + replayFile + "')");
        @task = dataFileMgr.Replay_Load(replayFile);
        if (task is null) {
            WriteOut(donePath, jobId, runId, false,
                     "Replay_Load returned null",
                     frames, ghostResult, false, "load_error");
            app.BackToMainMenu();
            return;
        }

        int loadDeadline = Time::Stamp + REPLAY_LOAD_WAIT_SECONDS;
        while (task.IsProcessing && Time::Stamp < loadDeadline
               && Time::Stamp < deadlineUnix) {
            yield();
            sleep(100);
        }

        // Guard BOTH the task verdict and the array length. The
        // published reference plugin indexes Ghosts[0] blind; a replay
        // that loads but holds no ghost then takes the plugin down.
        if (task.IsProcessing || !task.HasSucceeded
            || task.Ghosts.Length == 0) {
            string why = task.IsProcessing
                ? "Replay_Load still processing after "
                  + REPLAY_LOAD_WAIT_SECONDS + "s"
                : (task.HasSucceeded
                    ? "replay loaded but contains no ghost"
                    : "Replay_Load failed: " + task.ErrorType
                      + " / " + task.ErrorCode);
            dataFileMgr.TaskResult_Release(task.Id);
            WriteOut(donePath, jobId, runId, false, why,
                     frames, ghostResult, false, "load_error");
            app.BackToMainMenu();
            return;
        }
        @ghost = task.Ghosts[0];
    } else {
        ghostSource = "map_author_ghost";
        if (app.RootMap is null) {
            WriteOut(donePath, jobId, runId, false,
                     "no replay_file given and RootMap is null",
                     frames, ghostResult, false, "load_error");
            app.BackToMainMenu();
            return;
        }
        @ghost = dataFileMgr.Map_GetAuthorGhost(app.RootMap);
        if (ghost is null) {
            // Measured on the corpus: only 334 of 545 Stadium2020
            // gold-set maps embed one. This is an expected outcome for
            // a sizeable minority, not a malfunction.
            WriteOut(donePath, jobId, runId, false,
                     "map has no embedded author ghost",
                     frames, ghostResult, false, "no_author_ghost");
            app.BackToMainMenu();
            return;
        }
    }

    @ghostResult = GhostResultToJson(ghost);
    log("ghost via " + ghostSource + ": " + string(ghost.Nickname)
        + " time=" + int(ghost.Result.Time) + "ms");

    // IsGhostLayer = true: play it as an overlay ghost rather than as a
    // competing racer.
    MwId ghostInstance = pgs.Ghost_Add(ghost, true);

    // -----------------------------------------------------------------
    // Sampling loop. Reads the GHOST, not the operator's car.
    // -----------------------------------------------------------------
    int playbackDeadline = Time::Stamp + PLAYBACK_WAIT_SECONDS;
    int clockOrigin = int(pgs.Now);
    bool spawned = false;
    bool replayOver = false;

    // Diagnostics for the ghost-never-spawns case, which is otherwise
    // indistinguishable between "the ghost failed" and "the race never
    // started so nothing could play". Tracks the OPERATOR's car, which
    // is emphatically not the telemetry source (see the v0.2 header),
    // purely to tell those two apart.
    bool playerSeen = false;
    int maxPlayerRaceTime = 0;

    // Index of the first frame where the ghost held a real position.
    //
    // This is the anchor that makes the ghost's own checkpoint splits
    // usable. Those splits are relative to RACE START; our t_ms is
    // relative to whenever this loop happened to begin, which is during
    // the countdown. Without an anchor the two clocks cannot be aligned
    // and the splits are unmappable to frames.
    //
    // The ghost is placed in the scene when the replay begins, so its
    // first non-origin position IS race start. Stays -1 only if the
    // ghost never appeared at all, where a guessed anchor would
    // silently corrupt every checkpoint index downstream.
    int movedFrameIndex = -1;

    while (Time::Stamp < playbackDeadline
           && Time::Stamp < deadlineUnix
           && frames.Length < uint(MAX_FRAMES)) {
        yield();

        int t = int(pgs.Now) - clockOrigin;
        vec3 p = pgs.Ghost_GetPosition(ghostInstance);

        // Diagnostic only. Never sampled into a Frame.
        if (!spawned && playground.GameTerminals.Length > 0) {
            auto smPlayer = cast<CSmPlayer>(
                playground.GameTerminals[0].GUIPlayer);
            if (smPlayer !is null) {
                playerSeen = true;
                auto papi = cast<CSmScriptPlayer>(smPlayer.ScriptAPI);
                if (papi !is null && int(papi.CurrentRaceTime)
                        > maxPlayerRaceTime) {
                    maxPlayerRaceTime = int(papi.CurrentRaceTime);
                }
            }
        }

        Frame@ f = Frame();
        f.t_ms = t;
        f.x = p.x;
        f.y = p.y;
        f.z = p.z;
        frames.InsertLast(f);

        // Spawn detection. Ghost_IsReplayOver is true before the replay
        // starts, so trusting it from frame 0 ends every job empty.
        //
        // Do NOT restructure this as a timeout that "gives up waiting"
        // and lets the loop proceed as if started. A previous build did
        // exactly that and it cost 7 of 20 pilot maps: once the fallback
        // fired, this branch stopped running, so a ghost that appeared
        // later was never anchored even though it drove the whole track
        // and produced hundreds of good frames.
        if (!spawned
            && (Math::Abs(p.x) > ORIGIN_EPSILON_M
                || Math::Abs(p.y) > ORIGIN_EPSILON_M
                || Math::Abs(p.z) > ORIGIN_EPSILON_M)) {
            spawned = true;
            movedFrameIndex = int(frames.Length) - 1;
            log("ghost entered the scene at t=" + t + "ms (frame "
                + movedFrameIndex + ")");
        }

        if (spawned) {
            if (pgs.Ghost_IsReplayOver(ghostInstance)) {
                replayOver = true;
                break;
            }
        } else if (t > GHOST_SPAWN_WAIT_MS) {
            // Never appeared. Bail rather than burn the full playback
            // ceiling on a ghost that is not coming.
            break;
        }
        sleep(SAMPLE_PERIOD_MS);
    }

    string exitReason;
    if (frames.Length >= uint(MAX_FRAMES)) {
        exitReason = "max_frames_capped";
    } else if (replayOver) {
        exitReason = "finished";
    } else if (!spawned) {
        exitReason = "ghost_never_spawned";
    } else {
        exitReason = "playback_timeout";
    }

    log("sampled " + frames.Length + " frames, exit=" + exitReason
        + " player_seen=" + playerSeen
        + " player_race_time=" + maxPlayerRaceTime + "ms");
    ghostResult["diag_player_seen"] = playerSeen;
    ghostResult["diag_player_race_time_ms"] = maxPlayerRaceTime;

    pgs.Ghost_Remove(ghostInstance);
    // Only the Replay_Load path allocates a task result. The
    // author-ghost path has nothing to release.
    if (task !is null) {
        dataFileMgr.TaskResult_Release(task.Id);
    }
    app.BackToMainMenu();

    ghostResult["source"] = ghostSource;
    WriteOut(donePath, jobId, runId, true, "",
             frames, ghostResult, replayOver, exitReason, movedFrameIndex);
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
    const string &in donePath,
    int jobId, const string &in runId,
    bool loadSuccess, const string &in loadError,
    array<Frame@> &in frames,
    Json::Value@ ghostResult,
    bool finished,
    const string &in exitReason,
    int startFrameIndex = -1
) {
    Json::Value@ doc = Json::Object();
    doc["protocol"] = PROTOCOL;
    doc["job_id"] = jobId;
    doc["run_id"] = runId;
    doc["load_success"] = loadSuccess;
    if (loadError.Length > 0) {
        doc["load_error"] = loadError;
    }
    doc["plugin_version"] = PLUGIN_VERSION;
    doc["sample_period_ms"] = SAMPLE_PERIOD_MS;
    doc["finished"] = finished;
    doc["exit_reason"] = exitReason;
    doc["frame_count"] = int(frames.Length);
    doc["ghost"] = ghostResult;
    // -1 means "no movement anchor". Consumers MUST treat that as
    // "checkpoint splits cannot be aligned to frames" rather than
    // defaulting to frame 0.
    doc["start_frame_index"] = startFrameIndex;

    Json::Value@ framesArr = Json::Array();
    for (uint i = 0; i < frames.Length; i++) {
        framesArr.Add(frames[i].ToJson());
    }
    doc["frames"] = framesArr;

    // Compatibility shims so the rig server's existing aggregator
    // (which knows AIRouteTelemetry's shape) doesn't crash on a missing
    // field. checkpoint_times_ms is now genuinely populated: it comes
    // from the ghost's own race result, not from our sampling.
    doc["spawn_ok"] = loadSuccess;
    doc["validation_status"] = finished ? "Validated" : "Unknown";
    if (ghostResult.HasKey("checkpoint_times_ms")) {
        doc["checkpoint_times_ms"] = ghostResult["checkpoint_times_ms"];
    } else {
        doc["checkpoint_times_ms"] = Json::Array();
    }
    doc["driven_cells"] = Json::Array();

    Json::ToFile(donePath, doc);
    log("wrote " + donePath
        + " (load=" + loadSuccess
        + " frames=" + frames.Length
        + " finished=" + finished
        + " exit=" + exitReason + ")");
}


void log(const string &in msg) {
    trace("[AIReplayTelemetry] " + msg);
}
