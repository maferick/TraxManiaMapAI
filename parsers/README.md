# parsers/

.NET wrapper binaries that live **outside** the Python pipeline.
The Python side (`src/parsers/`) calls this wrapper via subprocess
per the wire protocol in `src/parsers/README.md`.

Keeping the .NET runtime isolated here is a hard architectural
constraint (`CLAUDE.md`): the Python pipeline must not link
.NET directly.

## Current wrappers

| Directory            | Language | Purpose                                          |
|----------------------|----------|--------------------------------------------------|
| `gbx-wrapper/`       | C# / .NET 8 | Parses `*.Map.Gbx` and `*.Replay.Gbx` via [GBX.NET](https://github.com/BigBang1112/gbx-net). |

## Building

Requires the .NET 8 SDK.

```bash
cd parsers/gbx-wrapper
dotnet restore
dotnet build -c Release
```

The published executable lands at:

```
parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper
```

Point `parsers.gbx.executable` in `config/settings.yaml` at that
path (or produce a self-contained publish via `dotnet publish` if
you need a portable binary).

## Why a separate project, not a NuGet reference from Python

Python can't directly reference NuGet packages. The GBX.NET library
is the upstream .NET parser; we consume it inside a thin C# wrapper
that exposes a **stable, language-agnostic JSON protocol over
stdin/stdout**. The Python side is shielded from both the .NET
runtime and any upstream API changes — bumping `GBX.NET` is a
wrapper-only change.

## Not committed to git

Per the repo's `.gitignore`, these directories are ignored under
`parsers/`:

- `_build/`, `bin/`, `obj/` — .NET build artifacts

Source files (`.cs`, `.csproj`, `.sln`) are committed.
