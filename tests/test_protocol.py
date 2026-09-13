"""Per-request protocol metadata rules for MCP 2026-07-28 and the legacy handshake.

These pin the contract the server routes on: which era a request belongs to,
what makes a modern request malformed, and what a finished result looks like.
"""

import unittest

from antigravity_mcp import protocol
from antigravity_mcp.protocol import ProtocolError


def modern_request(method="tools/list", meta=None):
    """A modern request, valid unless the caller overrides its _meta."""
    if meta is None:
        meta = {
            protocol.META_PROTOCOL_VERSION: "2026-07-28",
            protocol.META_CLIENT_CAPABILITIES: {},
        }
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"_meta": meta}}


class TestRequestMeta(unittest.TestCase):
    def test_returns_meta(self):
        """A well-formed request yields its _meta dict."""
        self.assertEqual(
            protocol.request_meta(modern_request())[protocol.META_PROTOCOL_VERSION], "2026-07-28"
        )

    def test_missing_params(self):
        """A request with no params has no metadata, and that is not an error."""
        self.assertEqual(protocol.request_meta({"method": "ping"}), {})

    def test_non_dict_params(self):
        """JSON-RPC permits array params; they carry no _meta and must not raise."""
        self.assertEqual(protocol.request_meta({"params": [1, 2]}), {})

    def test_non_dict_meta(self):
        """A _meta that is not an object is unusable, not a crash."""
        self.assertEqual(protocol.request_meta({"params": {"_meta": "nope"}}), {})

    def test_non_dict_request(self):
        """Runs on unvalidated wire input, so even a non-object request is handled."""
        self.assertEqual(protocol.request_meta("not a request"), {})


class TestEraDetection(unittest.TestCase):
    def test_modern_when_version_declared(self):
        """The protocol version key is the era signal."""
        self.assertTrue(protocol.is_modern_request(modern_request()))

    def test_legacy_initialize(self):
        """A handshake request carries no _meta and is legacy."""
        self.assertFalse(
            protocol.is_modern_request(
                {"method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
            )
        )

    def test_meta_without_version_is_not_modern(self):
        """A progressToken alone does not make a request modern."""
        self.assertFalse(protocol.is_modern_request({"params": {"_meta": {"progressToken": 1}}}))


class TestModernNegotiation(unittest.TestCase):
    def test_valid_request(self):
        """A complete modern request negotiates the version it declared."""
        self.assertEqual(protocol.negotiate_modern_version(modern_request()), "2026-07-28")

    def test_legacy_version_via_modern_meta(self):
        """A version this server supports is acceptable however it was declared."""
        request = modern_request(
            meta={
                protocol.META_PROTOCOL_VERSION: "2024-11-05",
                protocol.META_CLIENT_CAPABILITIES: {},
            }
        )
        self.assertEqual(protocol.negotiate_modern_version(request), "2024-11-05")

    def test_version_not_a_string(self):
        """A malformed version is refused rather than coerced."""
        request = modern_request(
            meta={
                protocol.META_PROTOCOL_VERSION: 20260728,
                protocol.META_CLIENT_CAPABILITIES: {},
            }
        )
        with self.assertRaises(ProtocolError) as caught:
            protocol.negotiate_modern_version(request)
        self.assertEqual(caught.exception.code, protocol.INVALID_PARAMS)

    def test_missing_client_capabilities(self):
        """Capabilities are required: a server must not rely on one never declared."""
        request = modern_request(meta={protocol.META_PROTOCOL_VERSION: "2026-07-28"})
        with self.assertRaises(ProtocolError) as caught:
            protocol.negotiate_modern_version(request)
        self.assertEqual(caught.exception.code, protocol.INVALID_PARAMS)

    def test_client_capabilities_not_a_dict(self):
        """A capabilities field of the wrong shape is as unusable as a missing one."""
        request = modern_request(
            meta={
                protocol.META_PROTOCOL_VERSION: "2026-07-28",
                protocol.META_CLIENT_CAPABILITIES: ["tools"],
            }
        )
        with self.assertRaises(ProtocolError) as caught:
            protocol.negotiate_modern_version(request)
        self.assertEqual(caught.exception.code, protocol.INVALID_PARAMS)

    def test_unknown_version_names_what_is_supported(self):
        """-32022 must list real alternatives so the client can retry, not just fail."""
        request = modern_request(
            meta={
                protocol.META_PROTOCOL_VERSION: "1900-01-01",
                protocol.META_CLIENT_CAPABILITIES: {},
            }
        )
        with self.assertRaises(ProtocolError) as caught:
            protocol.negotiate_modern_version(request)
        error = caught.exception
        self.assertEqual(error.code, protocol.UNSUPPORTED_PROTOCOL_VERSION)
        self.assertEqual(error.data["requested"], "1900-01-01")
        self.assertEqual(error.data["supported"], list(protocol.SUPPORTED_VERSIONS))


class TestProtocolErrorRendering(unittest.TestCase):
    def test_includes_data_when_present(self):
        """Error data reaches the client intact."""
        rendered = ProtocolError(-32022, "nope", {"supported": []}).to_error_object()
        self.assertEqual(rendered, {"code": -32022, "message": "nope", "data": {"supported": []}})

    def test_omits_data_when_absent(self):
        """An absent data member is left out, not sent as null for clients to interpret."""
        self.assertNotIn("data", ProtocolError(-32602, "bad").to_error_object())


class TestClientInfo(unittest.TestCase):
    def test_present(self):
        """Client identity is surfaced for logging when sent."""
        request = modern_request(
            meta={
                protocol.META_PROTOCOL_VERSION: "2026-07-28",
                protocol.META_CLIENT_CAPABILITIES: {},
                protocol.META_CLIENT_INFO: {"name": "C", "version": "1"},
            }
        )
        self.assertEqual(protocol.client_info(request)["name"], "C")

    def test_absent(self):
        """It is optional, so its absence is None rather than an error."""
        self.assertIsNone(protocol.client_info(modern_request()))


class TestLegacyNegotiation(unittest.TestCase):
    def test_supported_legacy_version_is_honoured(self):
        """A legacy client gets the legacy version it asked for."""
        self.assertEqual(protocol.negotiate_legacy_version("2025-06-18"), "2025-06-18")

    def test_missing_version(self):
        """No stated preference falls back to the baseline revision."""
        self.assertEqual(protocol.negotiate_legacy_version(None), protocol.DEFAULT_LEGACY_VERSION)

    def test_unknown_version(self):
        """An unrecognised version negotiates down rather than failing the handshake."""
        self.assertEqual(
            protocol.negotiate_legacy_version("1999-01-01"), protocol.DEFAULT_LEGACY_VERSION
        )

    def test_modern_version_is_never_promised(self):
        """Using the handshake proves the client is not speaking the per-request
        protocol, so naming a modern version would promise what it cannot use."""
        self.assertEqual(
            protocol.negotiate_legacy_version("2026-07-28"), protocol.DEFAULT_LEGACY_VERSION
        )


class TestCompleteResult(unittest.TestCase):
    def test_marks_result_complete(self):
        """Every result must state its type."""
        self.assertEqual(protocol.complete_result({}, "s", "1")["resultType"], "complete")

    def test_embeds_server_identity(self):
        """serverInfo travels in result _meta under the reserved key."""
        meta = protocol.complete_result({}, "antigravity-mcp", "1.1.0")["_meta"]
        self.assertEqual(
            meta[protocol.META_SERVER_INFO], {"name": "antigravity-mcp", "version": "1.1.0"}
        )

    def test_does_not_mutate_the_payload(self):
        """Payloads are often module constants — the tool catalog among them — and
        must not accumulate per-response fields."""
        payload = {"tools": []}
        protocol.complete_result(payload, "s", "1")
        self.assertEqual(payload, {"tools": []})

    def test_preserves_existing_meta(self):
        """An unrelated _meta key set by a handler survives."""
        result = protocol.complete_result({"_meta": {"progressToken": 7}}, "s", "1")
        self.assertEqual(result["_meta"]["progressToken"], 7)
        self.assertIn(protocol.META_SERVER_INFO, result["_meta"])

    def test_preserves_input_required_result_type(self):
        """An MRTR interim result says the request is NOT finished; overwriting it
        with "complete" would tell the client the opposite of what was meant."""
        result = protocol.complete_result({"resultType": "input_required"}, "s", "1")
        self.assertEqual(result["resultType"], "input_required")


class TestVersionTables(unittest.TestCase):
    def test_eras_are_disjoint_and_complete(self):
        """A version belongs to exactly one era, and every supported one is classified."""
        self.assertEqual(protocol.MODERN_VERSIONS & protocol.LEGACY_VERSIONS, frozenset())
        self.assertEqual(
            protocol.MODERN_VERSIONS | protocol.LEGACY_VERSIONS, set(protocol.SUPPORTED_VERSIONS)
        )

    def test_newest_version_is_first(self):
        """The advertised order is what a client with no preference reaches for."""
        self.assertEqual(protocol.SUPPORTED_VERSIONS[0], "2026-07-28")


if __name__ == "__main__":
    unittest.main(verbosity=2)
