// NadeoPak (.pak) capability probe (#217-M2a).
//
// TM2020 ships its game data in Packs/Stadium.pak rather than loose
// .Block.Gbx files. Before we commit to a walker/cataloguer design we
// need to know, empirically: does GBX.NET.PAK open the operator's
// Stadium.pak without keys, and does it enumerate the .Block.Gbx
// entries we need for M2 (per-block footprint extraction)?
//
// This verb answers that question and nothing else. It does NOT
// extract or decompress entries — just reads the directory.
//
// Input (stdin, one line of text): absolute path to a .pak file.
//
// Output (wrapper protocol v1 envelope):
//   success → {"status":"success","parser_version":"x.y.z","output":{
//     "pak_path":        "/abs/path/Stadium.pak",
//     "pak_version":     int,                  // 6 = modern TM2020 format
//     "title_id":        "TMStadium" | null,
//     "is_header_encrypted": bool,
//     "is_data_private": bool,
//     "file_count":      int,
//     "block_gbx_count": int,                  // ends with .Block.Gbx (case-insensitive)
//     "block_gbx_sample": [                    // first N block entries, for sanity
//       {"path": "GameCtnBlockInfo/...", "size": int, "compressed_size": int,
//        "is_encrypted": bool, "class_id": "0x......"}
//     ]
//   }}
//
// The goal is a yes/no on "can we read Stadium.pak" plus enough
// metadata to design M2b. If the operator's pak needs a key we expect
// a structured error here, not an exception.

using GBX.NET.PAK;

namespace TraxMania.GbxWrapper;

internal static class PakProbe
{
    // Cap the sample list — we want confirmation the entries are
    // enumerable, not a dump of every block path in the pack.
    private const int SampleSize = 20;

    public static Dictionary<string, object?> ProbeFromPath(string path)
    {
        if (!File.Exists(path))
            throw new FileNotFoundException($"pak file missing: {path}");

        using var stream = File.OpenRead(path);
        // key=null: best-effort read. Public paks should expose their
        // directory under the library's built-in header key; private
        // paks will throw and we'll surface that via ClassifyError.
        var pak = Pak.Parse(stream, key: null, computeKey: true);

        string? titleId = null;
        bool isDataPrivate = false;
        if (pak is Pak6 p6)
        {
            titleId = p6.TitleId;
            isDataPrivate = p6.IsDataPrivate;
        }

        var files = pak.Files;
        var blockEntries = new List<PakFile>();
        foreach (var kvp in files)
        {
            if (kvp.Value.Name.EndsWith(".Block.Gbx", StringComparison.OrdinalIgnoreCase))
            {
                blockEntries.Add(kvp.Value);
            }
        }

        var sample = new List<Dictionary<string, object?>>();
        foreach (var f in blockEntries.Take(SampleSize))
        {
            var fullPath = string.IsNullOrEmpty(f.FolderPath)
                ? f.Name
                : $"{f.FolderPath}/{f.Name}";
            sample.Add(new Dictionary<string, object?>
            {
                ["path"] = fullPath,
                ["size"] = f.UncompressedSize,
                ["compressed_size"] = f.CompressedSize,
                ["is_encrypted"] = f.IsEncrypted,
                ["class_id"] = $"0x{f.ClassId:X8}",
            });
        }

        return new Dictionary<string, object?>
        {
            ["pak_path"] = path,
            ["pak_version"] = pak.Version,
            ["title_id"] = titleId,
            ["is_header_encrypted"] = pak.IsHeaderEncrypted,
            ["is_data_private"] = isDataPrivate,
            ["file_count"] = files.Count,
            ["block_gbx_count"] = blockEntries.Count,
            ["block_gbx_sample"] = sample,
        };
    }
}
