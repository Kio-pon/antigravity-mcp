"""Per-request protocol metadata for MCP 2026-07-28, alongside the older handshake.

The 2026-07-28 revision made MCP stateless: there is no `initialize` exchange
any more, and every request instead declares its own protocol version and client
capabilities in `_meta`. Earlier revisions ("legacy", 2025-11-25 and before)
open with a handshake and carry nothing per request.

This server is what the spec calls *dual-era* — it answers both. The era is a
property of each request, not of the connection: a request carrying
`_meta['io.modelcontextprotocol/protocolVersion']` is modern, and an
`initialize` request is legacy. Keeping that decision here, in pure functions
over plain dicts, is what lets the server itself stay a simple router.
"""

from typing import Any, Optional

# Versions whose clients declare everything per request.
MODERN_VERSIONS = frozenset({"2026-07-28"})

# Versions whose clients expect an `initialize` handshake.
LEGACY_VERSIONS = frozenset({"2025-06-18", "2025-03-26", "2024-11-05"})

# Newest first: this is the order advertised to clients, and the first entry is
# what a client with no preference should reach for.
SUPPORTED_VERSIONS = ("2026-07-28", "2025-06-18", "2025-03-26", "2024-11-05")

# What an `initialize` handshake negotiates down to when the client asks for
# something this server cannot serve through that handshake.
DEFAULT_LEGACY_VERSION = "2024-11-05"

# Reserved `_meta` keys. The `io.modelcontextprotocol/` prefix is reserved by
# the specification, so these names are fixed and must not be invented locally.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
META_LOG_LEVEL = "io.modelcontextprotocol/logLevel"

INVALID_PARAMS = -32602

# -32020..-32099 is reserved for the specification, and an implementation must
# never emit a code from that range that the spec has not defined.
HEADER_MISMATCH = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022


class ProtocolError(Exception):
    """A request that cannot be served as sent, carrying its JSON-RPC error."""

    def __init__(self, code: int, message: str, data: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_error_object(self) -> dict[str, Any]:
        """Render the JSON-RPC `error` member.

        `data` is omitted entirely when absent rather than sent as null, since
        the spec makes it optional and a null would be a value clients must
        then interpret.
        """
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


def request_meta(request: dict[str, Any]) -> dict[str, Any]:
    """Return a request's `_meta`, or an empty dict when it has none.

    Never raises. This runs on unvalidated input straight off the wire, where
    `params` or `_meta` may be absent or may not be objects at all, and callers
    need a dict to interrogate either way.
    """
    if not isinstance(request, dict):
        return {}
    params = request.get("params")
    if not isinstance(params, dict):
        return {}
    meta = params.get("_meta")
    return meta if isinstance(meta, dict) else {}


def is_modern_request(request: dict[str, Any]) -> bool:
    """True when the request declares a protocol version in `_meta`.

    This single key is the era signal. A modern client puts it on every request;
    a legacy client has no way to send it.
    """
    return META_PROTOCOL_VERSION in request_meta(request)


def negotiate_modern_version(request: dict[str, Any]) -> str:
    """Validate a modern request and return the protocol version it declared.

    Both the version and the client's capabilities are required on every modern
    request, and the spec is explicit that a request missing a required field is
    malformed and must be refused with -32602. Capabilities matter as much as
    the version: a server must not rely on a capability the client never
    declared, which is impossible to honour if the field is simply missing.

    An unknown version raises -32022 naming the versions actually on offer, so
    the client can retry with one of them instead of guessing.
    """
    meta = request_meta(request)

    version = meta.get(META_PROTOCOL_VERSION)
    if not isinstance(version, str):
        raise ProtocolError(
            INVALID_PARAMS,
            f"Missing or malformed required request metadata: '{META_PROTOCOL_VERSION}'.",
        )

    capabilities = meta.get(META_CLIENT_CAPABILITIES)
    if not isinstance(capabilities, dict):
        raise ProtocolError(
            INVALID_PARAMS,
            f"Missing or malformed required request metadata: '{META_CLIENT_CAPABILITIES}'.",
        )

    if version not in SUPPORTED_VERSIONS:
        raise ProtocolError(
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            {"supported": list(SUPPORTED_VERSIONS), "requested": version},
        )

    return version


def client_info(request: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The client's self-reported name and version, when it sent one.

    Optional, and self-reported: useful for logs and diagnostics, never for
    deciding what the server will do.
    """
    info = request_meta(request).get(META_CLIENT_INFO)
    return info if isinstance(info, dict) else None


def negotiate_legacy_version(requested: Optional[str]) -> str:
    """Pick the version to answer an `initialize` handshake with.

    A modern version is never returned here, even if the client asked for one.
    Using the handshake at all shows the client is not speaking the per-request
    protocol, so naming a modern version would promise behaviour it cannot use.
    """
    if isinstance(requested, str) and requested in LEGACY_VERSIONS:
        return requested
    return DEFAULT_LEGACY_VERSION


def server_meta(name: str, version: str) -> dict[str, Any]:
    """The server's identity, shaped for embedding in a result's `_meta`."""
    return {META_SERVER_INFO: {"name": name, "version": version}}


def complete_result(
    payload: dict[str, Any], server_name: str, server_version: str
) -> dict[str, Any]:
    """Return `payload` as a finished result: typed, and carrying server identity.

    Copies rather than mutates, because payloads are often built from module
    constants (the tool catalog among them) that must not acquire per-response
    fields.

    An existing `resultType` is left alone. A multi-round-trip result marked
    "input_required" is a deliberate statement that the request is *not*
    finished, and overwriting it with "complete" would tell the client the
    opposite of what the handler meant.
    """
    result = dict(payload)
    result.setdefault("resultType", "complete")

    existing_meta = result.get("_meta")
    meta = dict(existing_meta) if isinstance(existing_meta, dict) else {}
    meta.update(server_meta(server_name, server_version))
    result["_meta"] = meta
    return result
