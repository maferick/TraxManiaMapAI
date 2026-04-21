# src/parsers

GBX parser boundary. The Python pipeline never links the .NET runtime;
parsing runs in an external process.

## Transport decision (resolved PR 3)

**Subprocess-per-artifact** is the concrete PR 3 implementation
(`subprocess_parser.py`). One wrapper invocation per file.

Why subprocess and not a long-running HTTP service:

- simpler ops — no extra daemon, no port, no liveness check
- crash isolation is free — a wrapper crash cannot corrupt subsequent
  parses because the process is gone
- a new parser version is deployed by swapping the binary, no graceful
  shutdown dance

Why this is acceptable given cold-start cost:

- at the ingestion rate ceiling (1 rps default in
  `config/settings.example.yaml`), the HTTP/subprocess difference is
  within noise — ingestion is network-bound, not parse-bound
- the boundary is abstracted (`ParserClient` ABC), so a future
  `HttpParser` can slot in without changing the callers

A long-running HTTP mode is explicitly *not* implemented in PR 3.
When throughput justifies it (if Phase 1 ever runs ingestion in bulk
against cached artifacts rather than live TMX), add an `HttpParser`
that implements the same ABC.

## Wrapper implementation

The reference .NET wrapper ships in-tree at `parsers/gbx-wrapper/`. It
wraps [GBX.NET](https://github.com/BigBang1112/gbx-net) (`GBX.NET` +
`GBX.NET.LZO`) inside a thin C# 8 console app that implements the
protocol below. Build it with:

```bash
dotnet build parsers/gbx-wrapper -c Release
```

Then point `parsers.gbx.executable` in `config/settings.yaml` at
`parsers/gbx-wrapper/bin/Release/net8.0/GbxWrapper`.

## Wrapper protocol (v1)

The wrapper binary lives outside this repo (except for the reference
.NET one shipped in `parsers/gbx-wrapper/`). Its contract:

- **Invocation**: `<executable> <kind>` where `<kind>` is `map` or `replay`.
- **stdin**: one line — absolute path to the artifact file, `\n`-terminated.
- **stdout**: a single JSON object. Either

  ```json
  {"status": "success", "parser_version": "x.y.z", "output": { ... }}
  ```

  or

  ```json
  {"status": "error", "parser_version": "x.y.z",
   "error_code": "<taxonomy code>", "error_detail": "..."}
  ```

- **Exit code**: `0` for any structured outcome (success or reported
  error). Non-zero only for process-level failure (wrapper crashed
  before it could report). Python treats non-zero as
  `ParseErrorCode.WRAPPER_CRASH`.

- **Error codes** must come from the closed taxonomy in
  `errors.py::ParseErrorCode`. Unknown codes are mapped to
  `ParseErrorCode.UNKNOWN`.

## Error taxonomy

Closed vocabulary in `errors.py`. Matches the SQL ENUM declaration in
`migrations/mariadb/003_maps.sql`. Adding a value is a schema
migration plus an enum update — don't invent new codes in the wrapper.

`is_transient(code)` decides whether a failure is retry-worthy. The
ingestion layer uses this to route errors between immediate retry,
backoff retry, and permanent failure.

## Not implemented here

- the wrapper binary itself (lives in a separate .NET build pipeline)
- parsing-result schema validation (the concrete map/replay object
  shape emerges in PR 4/5 once we have a real wrapper to compare
  against)
