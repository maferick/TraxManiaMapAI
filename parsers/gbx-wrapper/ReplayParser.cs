// Replay-side parser. Produces the JSON shape required by
// src/replay/telemetry.py::from_dict (ReplayTelemetry schema v1).
//
// Shape:
//   {
//     "schema_version":            1,
//     "source_replay_id":          string,
//     "sample_rate_hz":            int,
//     "player_login":              string?,
//     "finish_time_ms":            int?,
//     "samples": [
//       { "time_ms": int, "x": f, "y": f, "z": f,
//         "vx": f, "vy": f, "vz": f },
//       ...
//     ],
//     "checkpoint_sample_indices": int[],
//     "restart_sample_indices":    int[]
//   }
//
// GBX.NET exposes ghost-sample access through CGameGhost.SampleData.
// The exact field names vary across major versions; this scaffold
// uses reflection-free access where stable and falls back to an
// empty samples array when a wrapper-version / GBX.NET-version skew
// is encountered. The telemetry sidecar is always emitted alongside
// any successful parse — downstream cleaning interprets an empty
// samples array as "no telemetry", which the incomplete rule
// rejects appropriately.

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

        var firstGhost = replay.Ghosts?.Count > 0 ? replay.Ghosts[0] : null;
        if (firstGhost is not null)
        {
            playerLogin = firstGhost.GhostLogin ?? firstGhost.GhostNickname;
            // GBX.NET exposes RaceTime on ghosts as TimeInt32?. Its
            // TotalMilliseconds accessor yields a double we coerce to int.
            if (firstGhost.RaceTime is { } rt)
            {
                finishTimeMs = (int)rt.TotalMilliseconds;
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
        };
    }

    // GBX.NET's CGameGhost.SampleData.Samples shape is documented in the
    // upstream README. Each sample exposes Time, Position, Velocity.
    // Guard against null SampleData for header-only ghosts.
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
            int timeMs = (int)s.Time.TotalMilliseconds;
            into.Add(new Dictionary<string, object?>
            {
                ["time_ms"] = timeMs,
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
