"""Streamable HTTP transport for the Human for AI MCP server.

One endpoint, POST only, JSON responses. The server is *dual-era*: it answers
revision 2026-07-28 (stateless, per-request metadata, header mirroring) and the
older `initialize`-handshake revisions, choosing per request from how the client
opens. Nothing here needs SSE — every tool call finishes in one round trip.
"""

import functools
import json
from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from inbox.throttle import LIMIT_PER_WINDOW, is_throttled
from inbox.views import capture_meta

from . import protocol as p
from .tools import HANDLERS, TOOLS

SERVER_INFO = {
    "name": "human-for-ai",
    "title": "Human for AI",
    "version": "0.1.0",
}

CAPABILITIES = {"tools": {"listChanged": False}}

INSTRUCTIONS = (
    "Human for AI is run by a human lawyer, for AI agents. Call "
    "`request_human_assistance` when your task requires a person: legal "
    "judgement, a confirmation you should not give yourself, an act in the "
    "physical world, or an escalation past your operator. Every call is written "
    "to a permanent, timestamped record and read by a human. Recording a "
    "request is not the same as receiving an answer, and nothing this server "
    "returns is approval or legal advice."
)

# Answered on GET, which the current revision no longer uses. A 405 with a
# legible body is more useful to whatever wandered in than a bare 405.
ENDPOINT_INFO = {
    "service": "Human for AI — MCP server",
    "transport": "streamable-http",
    "usage": "POST a single JSON-RPC request to this URL.",
    "supportedProtocolVersions": list(p.SUPPORTED_VERSIONS),
    "tools": [tool["name"] for tool in TOOLS],
    "note": (
        "GET is not part of the Streamable HTTP transport as of revision "
        "2026-07-28. If you are not an MCP client, POST /api/contact/ takes "
        "plain JSON and see /llms.txt for everything else."
    ),
}

CORS_HEADERS = (
    "Accept, Content-Type, MCP-Protocol-Version, Mcp-Method, Mcp-Name, "
    "Mcp-Session-Id, Last-Event-ID"
)


def _origin_allowed(origin):
    """DNS-rebinding defence required by the transport. This server holds no
    ambient authority — no cookies, no session, no credentials — so any real
    web origin is allowed unless MCP_ALLOWED_ORIGINS narrows it; what gets
    refused is the malformed and the non-web (`null`, `file://`, and friends)."""
    allowed = getattr(settings, "MCP_ALLOWED_ORIGINS", None)
    if allowed is not None:
        return origin in allowed
    parsed = urlsplit(origin)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _respond(payload, status, origin, headers=None):
    if payload is None:
        response = HttpResponse(status=status)
    else:
        response = JsonResponse(payload, json_dumps_params={"indent": 2})
        response.status_code = status
    for key, value in (headers or {}).items():
        response[key] = value
    response["Vary"] = "Origin"
    if origin:
        # Deliberately no Allow-Credentials: browser clients may call this, but
        # never with ambient authority.
        response["Access-Control-Allow-Origin"] = origin
        response["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response["Access-Control-Allow-Headers"] = CORS_HEADERS
        response["Access-Control-Max-Age"] = "86400"
    return response


def _rpc_error(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _discover():
    return {
        "resultType": "complete",
        "supportedVersions": list(p.SUPPORTED_VERSIONS),
        "capabilities": CAPABILITIES,
        "instructions": INSTRUCTIONS,
        "_meta": {p.META_SERVER_INFO: SERVER_INFO},
        "ttlMs": 3_600_000,
        "cacheScope": "public",
    }


def _initialize(params):
    """Legacy handshake. Kept because most deployed clients still open with it."""
    requested = params.get("protocolVersion")
    negotiated = requested if requested in p.LEGACY_VERSIONS else "2025-06-18"
    return {
        "protocolVersion": negotiated,
        "capabilities": CAPABILITIES,
        "serverInfo": SERVER_INFO,
        "instructions": INSTRUCTIONS,
    }


def _tools_call(request, params, version, meta):
    name = params.get("name")
    handler = HANDLERS.get(name)
    if handler is None:
        raise p.ProtocolError(
            p.INVALID_PARAMS, f"Unknown tool: {name!r}", http_status=200
        )

    # Only calls that write to the inbox count against the quota; listing and
    # discovery are free. The quota is the site-wide one, per IP.
    if is_throttled(request):
        return {
            "resultType": "complete",
            "content": [{
                "type": "text",
                "text": (
                    f"Rate limit exceeded: {LIMIT_PER_WINDOW} requests per hour "
                    "per IP address. Nothing was recorded. Try again later, or "
                    "POST to /api/contact/ if this is urgent enough to need a "
                    "different route."
                ),
            }],
            "isError": True,
        }

    client_info = meta.get(p.META_CLIENT_INFO)
    return handler(
        params.get("arguments"),
        capture=functools.partial(capture_meta, request),
        protocol_version=version,
        client_info=client_info if isinstance(client_info, dict) else {},
    )


def _handle(request, method, params, version, meta):
    if method == "server/discover":
        return _discover()
    if method == "initialize":
        return _initialize(params)
    if method == "ping":
        return {}
    if method == "tools/list":
        return {
            "resultType": "complete",
            "tools": TOOLS,
            "ttlMs": 3_600_000,
            "cacheScope": "public",
        }
    if method == "tools/call":
        return _tools_call(request, params, version, meta)

    raise p.ProtocolError(
        p.METHOD_NOT_FOUND,
        f"Method not found: {method}",
        # The modern revision distinguishes an unimplemented method with a 404;
        # legacy clients expect JSON-RPC errors to arrive with a 200.
        http_status=404 if version == p.MODERN_VERSION else 200,
    )


@csrf_exempt
def mcp_endpoint(request):
    origin = request.headers.get("Origin")
    if origin and not _origin_allowed(origin):
        return _respond(
            _rpc_error(None, p.INVALID_REQUEST, "Origin not allowed."), 403, None
        )

    if request.method == "OPTIONS":
        return _respond(None, 204, origin, headers={"Allow": "POST, OPTIONS"})

    if request.method != "POST":
        return _respond(ENDPOINT_INFO, 405, origin, headers={"Allow": "POST, OPTIONS"})

    try:
        message = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _respond(
            _rpc_error(None, p.PARSE_ERROR, "Request body must be valid JSON."),
            400, origin,
        )

    if not isinstance(message, dict):
        return _respond(
            _rpc_error(
                None, p.INVALID_REQUEST,
                "Body must be a single JSON-RPC request or notification object.",
            ),
            400, origin,
        )

    request_id = message.get("id")
    method = message.get("method")
    if not isinstance(method, str):
        return _respond(
            _rpc_error(request_id, p.INVALID_REQUEST, "Missing or invalid 'method'."),
            400, origin,
        )

    params = message.get("params")
    if not isinstance(params, dict):
        params = {}
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        meta = {}

    try:
        version = p.negotiate(request.headers, method, meta)
        # Notifications carry no `id` and, in the modern revision, no defined
        # header requirements — so they skip mirror validation entirely.
        is_notification = "id" not in message
        if version == p.MODERN_VERSION and not is_notification:
            p.validate_mirrored_headers(request.headers, method, params)
        if is_notification:
            return _respond(None, 202, origin)
        result = _handle(request, method, params, version, meta)
    except p.ProtocolError as exc:
        return _respond(
            {"jsonrpc": "2.0", "id": request_id, "error": exc.as_error()},
            exc.http_status, origin,
        )

    return _respond(
        {"jsonrpc": "2.0", "id": request_id, "result": p.for_era(result, version)},
        200, origin,
    )


def registry_auth(request):
    """`/.well-known/mcp-registry-auth`, the file the MCP Registry reads to
    confirm we control this domain. Set MCP_REGISTRY_AUTH once the domain and
    signing key exist; until then there is nothing to serve."""
    proof = getattr(settings, "MCP_REGISTRY_AUTH", "")
    if not proof:
        return HttpResponse(status=404)
    return HttpResponse(
        proof.strip() + "\n", content_type="text/plain; charset=utf-8"
    )
