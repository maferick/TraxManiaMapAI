// Entry point for the GBX wrapper subprocess.
//
// Contract (must match src/parsers/README.md v1):
//   argv:           "map" | "replay"
//   stdin:          single line — absolute path to the artifact file
//   stdout:         a single JSON object (success or structured error)
//   exit code:      0 for any structured outcome (including reported errors)
//                   non-zero only for process-level failure before reporting
//
// Python-side mapping is in src/parsers/subprocess_parser.py.

using System.Text.Json;
using GBX.NET;
using GBX.NET.LZO;

namespace TraxMania.GbxWrapper;

public static class Program
{
    public const string ParserVersion = "0.1.0";

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = false,
    };

    public static int Main(string[] args)
    {
        if (args.Length != 1 ||
            (args[0] != "map" && args[0] != "replay"
             && args[0] != "diagnose-map" && args[0] != "diagnose-replay"))
        {
            Console.Error.WriteLine("usage: gbx-wrapper <map|replay|diagnose-map|diagnose-replay>");
            return 2;
        }

        // LZO decompresses Gbx bodies (maps, replays).
        Gbx.LZO = new Lzo();
        // ZLib decompresses ghost sample streams embedded in replays.
        // Maps don't strictly need it, but registering it upfront keeps
        // the wrapper protocol shape stable regardless of artifact kind.
        Gbx.ZLib = new GBX.NET.ZLib.ZLib();

        string? path;
        try
        {
            path = Console.In.ReadLine()?.Trim();
        }
        catch (IOException ex)
        {
            EmitError(ErrorCodes.IoError, $"stdin read failed: {ex.Message}");
            return 0;
        }

        if (string.IsNullOrWhiteSpace(path))
        {
            EmitError(ErrorCodes.IoError, "no path on stdin");
            return 0;
        }
        if (!File.Exists(path))
        {
            EmitError(ErrorCodes.IoError, $"file not found: {path}");
            return 0;
        }

        try
        {
            object payload = args[0] switch
            {
                "map" => MapParser.Parse(path),
                "replay" => ReplayParser.Parse(path),
                "diagnose-map" => Diagnose.Inspect(path),
                "diagnose-replay" => Diagnose.InspectReplay(path),
                _ => throw new InvalidOperationException("unreachable"),
            };
            EmitSuccess(payload);
            return 0;
        }
        catch (Exception ex)
        {
            EmitError(ClassifyError(ex), $"{ex.GetType().Name}: {ex.Message}");
            return 0;
        }
    }

    private static void EmitSuccess(object output)
    {
        var env = new Dictionary<string, object?>
        {
            ["status"] = "success",
            ["parser_version"] = ParserVersion,
            ["output"] = output,
        };
        Console.WriteLine(JsonSerializer.Serialize(env, JsonOpts));
    }

    private static void EmitError(string code, string detail)
    {
        var env = new Dictionary<string, object?>
        {
            ["status"] = "error",
            ["parser_version"] = ParserVersion,
            ["error_code"] = code,
            ["error_detail"] = detail,
        };
        Console.WriteLine(JsonSerializer.Serialize(env, JsonOpts));
    }

    // Map GBX.NET exception types onto the closed Python taxonomy. The
    // upstream library's exception hierarchy evolves between versions;
    // this switch is deliberately conservative — unknown shapes map to
    // CorruptBody (a permanent failure) rather than WrapperCrash (which
    // would trigger retries).
    private static string ClassifyError(Exception ex)
    {
        string name = ex.GetType().FullName ?? "";
        if (name.Contains("FileNotFoundException", StringComparison.Ordinal)
            || name.Contains("DirectoryNotFoundException", StringComparison.Ordinal))
        {
            return ErrorCodes.IoError;
        }
        if (name.Contains("Header", StringComparison.OrdinalIgnoreCase))
        {
            return ErrorCodes.CorruptHeader;
        }
        if (name.Contains("Lzo", StringComparison.OrdinalIgnoreCase)
            || name.Contains("Zlib", StringComparison.OrdinalIgnoreCase)
            || name.Contains("Compression", StringComparison.OrdinalIgnoreCase))
        {
            return ErrorCodes.CorruptBody;
        }
        if (name.Contains("NotAGbx", StringComparison.Ordinal)
            || name.Contains("NotSupported", StringComparison.Ordinal)
            || name.Contains("Unsupported", StringComparison.Ordinal))
        {
            return ErrorCodes.UnsupportedFormat;
        }
        if (ex is InvalidDataException)
        {
            return ErrorCodes.CorruptBody;
        }
        return ErrorCodes.Unknown;
    }
}
