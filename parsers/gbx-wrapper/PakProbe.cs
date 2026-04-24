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
//     "has_packlist":    bool,                 // packlist.dat found next to pak
//     "has_key":         bool,                 // key for this pak derived from packlist
//     "file_count":      int,
//     "block_gbx_count": int,                  // ends with .Block.Gbx (case-insensitive)
//     "block_gbx_sample": [                    // first N block entries, for sanity
//       {"path": "GameCtnBlockInfo/...", "size": int, "compressed_size": int,
//        "is_encrypted": bool, "class_id": "0x......"}
//     ]
//   }}
//
// #217-M2b: If a sibling ``packlist.dat`` is present (as shipped in
// TM2020's ``Packs/`` folder), its per-pak keys are derived using
// GBX.NET.PAK's PakList(TM salts) and the matching key is handed to
// Pak.Parse. Without it, the pak directory on TM2020's Stadium.pak
// reads as zero entries (header + data both private).

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

        // Look for a sibling packlist.dat (TM2020 ships one in the
        // game's Packs/ folder). Parse it with TM salts if present;
        // the resulting dict maps pak-id → decryption key bytes.
        var pakDir = Path.GetDirectoryName(path) ?? "";
        var packlistPath = Path.Combine(pakDir, PakList.FileName);
        byte[]? pakKey = null;
        bool hasPacklist = File.Exists(packlistPath);
        if (hasPacklist)
        {
            var packlist = PakList.Parse(packlistPath, PakListGame.TM);
            var pakId = Path.GetFileNameWithoutExtension(path);
            packlist.ToKeyInfoDictionary().TryGetValue(pakId, out pakKey);
        }

        using var stream = File.OpenRead(path);
        // With a derived key, Pak.Parse decrypts the directory. Without
        // one, we still open best-effort — some paks expose metadata
        // under the library's built-in header key even when private.
        var pak = Pak.Parse(stream, key: pakKey, computeKey: true);

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
            ["has_packlist"] = hasPacklist,
            ["has_key"] = pakKey is not null,
            ["file_count"] = files.Count,
            ["block_gbx_count"] = blockEntries.Count,
            ["block_gbx_sample"] = sample,
        };
    }
}
