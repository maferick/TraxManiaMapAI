// Error taxonomy strings. Must stay in lockstep with
// src/parsers/errors.py::ParseErrorCode on the Python side.
// Adding or removing a value is a cross-repo change.

namespace TraxMania.GbxWrapper;

internal static class ErrorCodes
{
    public const string None = "none";
    public const string GbxReadError = "gbx_read_error";
    public const string UnsupportedFormat = "unsupported_format";
    public const string CorruptHeader = "corrupt_header";
    public const string CorruptBody = "corrupt_body";
    public const string UnknownBlockType = "unknown_block_type";
    public const string WrapperTimeout = "wrapper_timeout";
    public const string WrapperCrash = "wrapper_crash";
    public const string IoError = "io_error";
    public const string Unknown = "unknown";
}
