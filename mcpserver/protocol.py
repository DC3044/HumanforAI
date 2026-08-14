"""Wire-level pieces of the Model Context Protocol.

Everything here is transport- and tool-agnostic: version constants, JSON-RPC
error shapes, and the header-mirroring rules that revision 2026-07-28 added to
the Streamable HTTP binding.
"""

import base64
import re

# The revision that made MCP stateless: no `initialize` handshake, no sessions,
# no GET stream. Every request carries its own metadata.
MODERN_VERSION = "2026-07-28"

# Revisions that still expect an `initialize` handshake. We answer these too,
# because that is what most deployed clients speak today.
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")

SUPPORTED_VERSIONS = (MODERN_VERSION, *LEGACY_VERSIONS)

# Version assumed when a client sends no MCP-Protocol-Version header at all;
# the header was only introduced in 2025-06-18.
ASSUMED_LEGACY_VERSION = "2025-03-26"

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# Standard JSON-RPC codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP-allocated codes.
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022

# Keys that only exist in the modern result schema; stripped before answering a
# legacy client, which validates results against the older shape.
MODERN_ONLY_RESULT_KEYS = ("resultType", "ttlMs", "cacheScope")

_SENTINEL = re.compile(r"^=\?base64\?(.*)\?=$")


class ProtocolError(Exception):
    """A JSON-RPC error that also fixes the HTTP status of the response."""

    def __init__(self, code, message, data=None, http_status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        self.http_status = http_status

    def as_error(self):
        error = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


def decode_header_value(raw):
    """Undo the `=?base64?...?=` sentinel encoding clients use for header values
    that are not safely representable in ASCII. Returns None if the sentinel is
    present but the payload does not decode."""
    match = _SENTINEL.match(raw)
    if not match:
        return raw
    try:
        return base64.b64decode(match.group(1), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def negotiate(headers, method, meta):
    """Decide which era of the protocol this request is speaking.

    Returns the protocol version string. `initialize` always selects a legacy
    version, because that method does not exist in the modern revision.
    """
    header_version = headers.get("MCP-Protocol-Version")

    if method == "initialize":
        # A client that handshakes is legacy by definition, whatever its header
        # claims — `initialize` does not exist in the modern revision.
        if header_version is None or header_version == MODERN_VERSION:
            return ASSUMED_LEGACY_VERSION
        return header_version

    if header_version is None:
        # Permitted fallback: pre-2025-06-18 clients did not send the header.
        return ASSUMED_LEGACY_VERSION

    if header_version not in SUPPORTED_VERSIONS:
        raise ProtocolError(
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            data={"supported": list(SUPPORTED_VERSIONS), "requested": header_version},
        )

    if header_version == MODERN_VERSION:
        body_version = meta.get(META_VERSION)
        if body_version != header_version:
            raise ProtocolError(
                HEADER_MISMATCH,
                f"Header mismatch: MCP-Protocol-Version header value "
                f"{header_version!r} does not match body value {body_version!r}",
            )

    return header_version


def validate_mirrored_headers(headers, method, params):
    """Revision 2026-07-28 mirrors `method` and `params.name`/`params.uri` into
    headers so intermediaries can route without parsing the body. If the two
    sources disagree, one of them is lying, so the request is refused."""
    header_method = headers.get("Mcp-Method")
    if header_method is None:
        raise ProtocolError(HEADER_MISMATCH, "Missing required header: Mcp-Method")
    if header_method != method:
        raise ProtocolError(
            HEADER_MISMATCH,
            f"Header mismatch: Mcp-Method header value {header_method!r} "
            f"does not match body value {method!r}",
        )

    if method not in ("tools/call", "resources/read", "prompts/get"):
        return

    body_name = params.get("uri") if method == "resources/read" else params.get("name")
    raw = headers.get("Mcp-Name")
    if raw is None:
        raise ProtocolError(HEADER_MISMATCH, "Missing required header: Mcp-Name")
    header_name = decode_header_value(raw)
    if header_name is None:
        raise ProtocolError(HEADER_MISMATCH, "Mcp-Name header value is not valid base64")
    if header_name != body_name:
        raise ProtocolError(
            HEADER_MISMATCH,
            f"Header mismatch: Mcp-Name header value {header_name!r} "
            f"does not match body value {body_name!r}",
        )


def for_era(result, version):
    """Results are written in the modern shape; older clients get the extra
    keys stripped rather than a second set of builders."""
    if version == MODERN_VERSION:
        return result
    return {k: v for k, v in result.items() if k not in MODERN_ONLY_RESULT_KEYS}
