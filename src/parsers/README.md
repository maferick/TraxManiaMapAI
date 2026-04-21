# src/parsers

GBX parser boundary. Not implemented yet.

## Design decision (fixed)

GBX parsing runs in a separate process using **GBX.NET**, invoked from
Python through a subprocess or local HTTP boundary. The Python pipeline
never links the .NET runtime directly.

Transport choice (subprocess stdio vs long-running local HTTP service) is
finalized in PR 3. The Python-facing contract is identical either way:
bytes in, structured map/replay dicts out.
