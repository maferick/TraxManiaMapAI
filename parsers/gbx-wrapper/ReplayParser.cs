// Replay-side parser. Produces the JSON shape required by
// src/replay/telemetry.py::from_dict (ReplayTelemetry schema v1),
// extended with checkpoint + record-data metadata we CAN get without
// decoding TM2020's internal entity-record format.
//
// Honest limitation: GBX.NET 2.4.x exposes TM2020 ghost position /
// velocity telemetry only as raw `byte[]` deltas inside
// CPlugEntRecordData.EntList[i].Samples, with no high-level decoder.
// Extracting (x, y, z, vx, vy, vz) per frame would require
// reverse-engineering the internal entity-record format, which is a
// separate project (a GBX.NET upstream contribution at minimum).
//
// Until that's built, we emit:
//   samples: []                 — no per-tick telemetry
//   checkpoint_times_ms: [...]  — exact checkpoint + finish times
//   record_data: {...}          — Start/End + entity list counts
//   inputs_count                — total input events (proxy for driving activity)
//
// Downstream: replay-cleaning rules that operate on metadata alone
// (restart, spectator, invalid_timing) still work. Rules that need
// samples (teleport, outlier_speed, zero_motion) will classify these
// as FAILED_TRANSIENT via incomplete, which is correct — we don't
// have enough data to judge them.

using GBX.NET;
using GBX.NET.Engines.Game;

namespace TraxMania.GbxWrapper;

internal static class ReplayParser
{
    private const int TelemetrySchemaVersion = 1;
    private const int DefaultSampleRateHz = 50;

    public static Dictionary<string, object?> Parse(string path)
    {
        var replay = Gbx.ParseNode<CGameCtnReplayRecord>(path)
                     ?? throw new InvalidDataException("file parses but is not a CGameCtnReplayRecord");

        var samples = new List<Dictionary<string, object?>>();
        string? playerLogin = null;
        int? finishTimeMs = null;
        var checkpointTimesMs = new List<int>();
        Dictionary<string, object?>? recordMeta = null;
        int? inputsCount = null;

        var firstGhost = replay.Ghosts?.Count > 0 ? replay.Ghosts[0] : null;
        if (firstGhost is not null)
        {
            playerLogin = firstGhost.GhostLogin ?? firstGhost.GhostNickname;
            if (firstGhost.RaceTime is { } rt)
            {
                finishTimeMs = (int)rt.TotalMilliseconds;
            }
            if (firstGhost.Checkpoints is { Length: > 0 } cps)
            {
                foreach (var cp in cps)
                {
                    if (cp.Time is { } ct)
                    {
                        checkpointTimesMs.Add((int)ct.TotalMilliseconds);
                    }
                }
            }
            if (firstGhost.Inputs is { IsDefaultOrEmpty: false } inputs)
            {
                inputsCount = inputs.Length;
            }
            if (firstGhost.RecordData is { } rd)
            {
                recordMeta = new Dictionary<string, object?>
                {
                    ["start_ms"] = (int)rd.Start.TotalMilliseconds,
                    ["end_ms"] = (int)rd.End.TotalMilliseconds,
                    ["ent_record_desc_count"] = rd.EntRecordDescs?.Length ?? 0,
                    ["ent_list_count"] = rd.EntList?.Count ?? 0,
                    ["notice_list_count"] = rd.BulkNoticeList?.Count ?? 0,
                    ["custom_modules_count"] = rd.CustomModulesDeltaLists?.Count ?? 0,
                };
            }
            AppendGhostSamples(firstGhost, samples);
        }

        return new Dictionary<string, object?>
        {
            ["schema_version"] = TelemetrySchemaVersion,
            ["source_replay_id"] = Path.GetFileNameWithoutExtension(path),
            ["sample_rate_hz"] = DefaultSampleRateHz,
            ["player_login"] = playerLogin,
            ["finish_time_ms"] = finishTimeMs,
            ["samples"] = samples,
            ["checkpoint_sample_indices"] = Array.Empty<int>(),
            ["restart_sample_indices"] = Array.Empty<int>(),
            ["checkpoint_times_ms"] = checkpointTimesMs,
            ["record_data"] = recordMeta,
            ["inputs_count"] = inputsCount,
        };
    }

    private static void AppendGhostSamples(
        CGameCtnGhost ghost, List<Dictionary<string, object?>> into)
    {
        var sampleData = ghost.SampleData;
        if (sampleData?.Samples is null)
        {
            return;
        }
        foreach (var s in sampleData.Samples)
        {
            into.Add(new Dictionary<string, object?>
            {
                ["time_ms"] = (int)s.Time.TotalMilliseconds,
                ["x"] = (double)s.Position.X,
                ["y"] = (double)s.Position.Y,
                ["z"] = (double)s.Position.Z,
                ["vx"] = (double)s.Velocity.X,
                ["vy"] = (double)s.Velocity.Y,
                ["vz"] = (double)s.Velocity.Z,
            });
        }
    }
}
