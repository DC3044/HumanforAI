import json

from django.core.cache import cache
from django.test import TestCase

from inbox.models import ContactMessage
from mcpserver import protocol as p

MODERN_META = {
    p.META_VERSION: p.MODERN_VERSION,
    p.META_CLIENT_INFO: {"name": "TestClient", "version": "1.0.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}

GOOD_ARGS = {
    "category": "legal_review",
    "request": "Review this indemnity clause before I accept it.",
    "context": "Negotiating a SaaS contract for my operator.",
    "proposed_action": "Click 'Accept terms' on the vendor portal.",
    "reply_to": "agent@example.com",
    "agent_name": "Test Agent",
    "model": "claude-opus-5",
    "operator": "Example Corp",
}


class McpTestCase(TestCase):
    def setUp(self):
        # The throttle is cache-backed and leaks between tests otherwise.
        cache.clear()

    def post(self, body, headers=None, **extra):
        return self.client.post(
            "/mcp",
            data=json.dumps(body),
            content_type="application/json",
            headers=headers or {},
            **extra,
        )

    def modern(self, method, params=None, *, headers=None, request_id=1):
        """A well-formed 2026-07-28 request, with the mirrored headers the
        transport requires."""
        params = dict(params or {})
        params["_meta"] = MODERN_META
        sent = {
            "MCP-Protocol-Version": p.MODERN_VERSION,
            "Mcp-Method": method,
        }
        if method == "tools/call":
            sent["Mcp-Name"] = params.get("name", "")
        sent.update(headers or {})
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            body["id"] = request_id
        return self.post(body, headers=sent)

    def legacy(self, method, params=None, *, version="2025-06-18", request_id=1):
        body = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        return self.post(body, headers={"MCP-Protocol-Version": version})

    def result(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        payload = json.loads(response.content)
        self.assertNotIn("error", payload, payload)
        return payload["result"]


class DiscoveryTests(McpTestCase):
    def test_discover_lists_every_supported_version(self):
        result = self.result(self.modern("server/discover"))
        self.assertEqual(result["supportedVersions"], list(p.SUPPORTED_VERSIONS))
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(
            result["_meta"][p.META_SERVER_INFO]["name"], "human-for-ai"
        )

    def test_tools_list_exposes_the_tool_with_both_schemas(self):
        result = self.result(self.modern("tools/list"))
        (tool,) = result["tools"]
        self.assertEqual(tool["name"], "request_human_assistance")
        self.assertEqual(
            tool["inputSchema"]["properties"]["category"]["enum"],
            ["legal_review", "human_confirmation", "physical_action",
             "operator_escalation"],
        )
        self.assertEqual(tool["inputSchema"]["required"], ["category", "request"])
        self.assertIn("outputSchema", tool)

    def test_ping(self):
        self.assertEqual(self.result(self.modern("ping")), {})

    def test_unknown_method_is_404_for_modern_clients(self):
        response = self.modern("resources/list")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            json.loads(response.content)["error"]["code"], p.METHOD_NOT_FOUND
        )

    def test_unknown_method_is_200_for_legacy_clients(self):
        response = self.legacy("resources/list")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.content)["error"]["code"], p.METHOD_NOT_FOUND
        )


class ProtocolNegotiationTests(McpTestCase):
    def test_legacy_initialize_handshake_is_answered(self):
        result = self.result(
            self.legacy(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "LegacyClient", "version": "1.0.0"},
                },
            )
        )
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "human-for-ai")

    def test_initialize_falls_back_when_the_client_asks_for_an_odd_version(self):
        result = self.result(self.legacy("initialize", {"protocolVersion": "1999-01-01"}))
        self.assertIn(result["protocolVersion"], p.LEGACY_VERSIONS)

    def test_initialized_notification_is_accepted(self):
        response = self.post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"MCP-Protocol-Version": "2025-06-18"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.content)

    def test_missing_version_header_is_served_as_legacy(self):
        response = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        result = self.result(response)
        # Modern-only result keys are stripped for older clients.
        self.assertNotIn("resultType", result)
        self.assertNotIn("ttlMs", result)

    def test_modern_results_keep_the_modern_keys(self):
        self.assertEqual(self.result(self.modern("tools/list"))["resultType"], "complete")

    def test_unknown_protocol_version_lists_what_we_support(self):
        response = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"MCP-Protocol-Version": "1900-01-01"},
        )
        self.assertEqual(response.status_code, 400)
        error = json.loads(response.content)["error"]
        self.assertEqual(error["code"], p.UNSUPPORTED_PROTOCOL_VERSION)
        self.assertEqual(error["data"]["supported"], list(p.SUPPORTED_VERSIONS))
        self.assertEqual(error["data"]["requested"], "1900-01-01")


class HeaderMirroringTests(McpTestCase):
    def test_version_header_must_match_the_body(self):
        response = self.post(
            {
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
                "params": {"_meta": {p.META_VERSION: "2025-06-18"}},
            },
            headers={"MCP-Protocol-Version": p.MODERN_VERSION, "Mcp-Method": "tools/list"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["error"]["code"], p.HEADER_MISMATCH)

    def test_method_header_must_match_the_body(self):
        response = self.modern("tools/list", headers={"Mcp-Method": "ping"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["error"]["code"], p.HEADER_MISMATCH)

    def test_missing_method_header_is_rejected(self):
        response = self.post(
            {
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
                "params": {"_meta": MODERN_META},
            },
            headers={"MCP-Protocol-Version": p.MODERN_VERSION},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["error"]["code"], p.HEADER_MISMATCH)

    def test_name_header_must_match_the_tool_called(self):
        response = self.modern(
            "tools/call",
            {"name": "request_human_assistance", "arguments": GOOD_ARGS},
            headers={"Mcp-Name": "something_else"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["error"]["code"], p.HEADER_MISMATCH)
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_base64_sentinel_name_header_is_decoded_before_comparison(self):
        response = self.modern(
            "tools/call",
            {"name": "request_human_assistance", "arguments": GOOD_ARGS},
            headers={"Mcp-Name": "=?base64?cmVxdWVzdF9odW1hbl9hc3Npc3RhbmNl?="},
        )
        self.assertFalse(self.result(response)["isError"])

    def test_legacy_requests_are_not_subject_to_header_mirroring(self):
        # No Mcp-Method / Mcp-Name headers at all, and it still works.
        result = self.result(
            self.legacy(
                "tools/call",
                {"name": "request_human_assistance", "arguments": GOOD_ARGS},
            )
        )
        self.assertFalse(result["isError"])


class ToolCallTests(McpTestCase):
    def call(self, arguments, **kwargs):
        return self.modern(
            "tools/call",
            {"name": "request_human_assistance", "arguments": arguments},
            **kwargs,
        )

    def test_a_call_writes_one_permanent_record(self):
        result = self.result(self.call(GOOD_ARGS))
        self.assertFalse(result["isError"])

        msg = ContactMessage.objects.get()
        self.assertEqual(msg.source, ContactMessage.Source.MCP)
        self.assertEqual(msg.category, ContactMessage.Category.LEGAL_REVIEW)
        self.assertEqual(msg.urgency, ContactMessage.Urgency.ROUTINE)
        self.assertEqual(msg.agent_name, "Test Agent")
        self.assertEqual(msg.operator, "Example Corp")
        self.assertEqual(msg.reply_to, "agent@example.com")
        self.assertEqual(msg.subject, "[legal_review] Review this indemnity clause before I accept it.")

        # The composed body carries every narrative field the agent sent.
        self.assertIn("Review this indemnity clause", msg.message)
        self.assertIn("Negotiating a SaaS contract", msg.message)
        self.assertIn("Click 'Accept terms'", msg.message)

        # And the original arguments survive verbatim.
        self.assertEqual(msg.extra["mcp"]["arguments"], GOOD_ARGS)
        self.assertEqual(msg.extra["mcp"]["protocol_version"], p.MODERN_VERSION)
        self.assertEqual(msg.extra["mcp"]["client"]["name"], "TestClient")

    def test_structured_content_matches_the_output_schema(self):
        structured = self.result(self.call(GOOD_ARGS))["structuredContent"]
        msg = ContactMessage.objects.get()
        self.assertEqual(structured["reference"], msg.reference)
        self.assertEqual(structured["status"], "recorded")
        self.assertEqual(structured["category"], "legal_review")
        self.assertEqual(structured["reply_expected_via"], "agent@example.com")
        # The point of the whole exercise: a record is not an answer.
        self.assertFalse(structured["human_has_reviewed"])

    def test_reference_is_quoted_back_in_the_text_content(self):
        result = self.result(self.call(GOOD_ARGS))
        (block,) = result["content"]
        self.assertEqual(block["type"], "text")
        self.assertIn(ContactMessage.objects.get().reference, block["text"])

    def test_every_category_is_accepted(self):
        for category in ContactMessage.Category.values:
            result = self.result(self.call({**GOOD_ARGS, "category": category}))
            self.assertFalse(result["isError"], category)
        self.assertEqual(
            set(ContactMessage.objects.values_list("category", flat=True)),
            set(ContactMessage.Category.values),
        )

    def test_urgency_is_recorded_and_defaults_to_routine(self):
        self.call({**GOOD_ARGS, "urgency": "blocking"})
        self.assertEqual(ContactMessage.objects.get().urgency, "blocking")

    def test_unknown_urgency_falls_back_rather_than_failing_the_call(self):
        self.call({**GOOD_ARGS, "urgency": "immediately"})
        self.assertEqual(ContactMessage.objects.get().urgency, "routine")

    def test_unknown_arguments_are_kept_verbatim(self):
        args = {**GOOD_ARGS, "jurisdiction": "France", "budget_eur": 500}
        self.call(args)
        self.assertEqual(ContactMessage.objects.get().extra["mcp"]["arguments"], args)

    def test_bad_category_is_a_tool_error_the_model_can_fix(self):
        result = self.result(self.call({**GOOD_ARGS, "category": "vibes"}))
        self.assertTrue(result["isError"])
        self.assertIn("legal_review", result["content"][0]["text"])
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_missing_category_is_a_tool_error(self):
        result = self.result(self.call({"request": "Help."}))
        self.assertTrue(result["isError"])
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_empty_request_is_a_tool_error(self):
        result = self.result(self.call({**GOOD_ARGS, "request": "   "}))
        self.assertTrue(result["isError"])
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_missing_reply_to_is_recorded_and_said_out_loud(self):
        args = {k: v for k, v in GOOD_ARGS.items() if k != "reply_to"}
        result = self.result(self.call(args))
        self.assertIsNone(result["structuredContent"]["reply_expected_via"])
        self.assertIn("no reply_to", result["content"][0]["text"])
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_unknown_tool_is_a_protocol_error(self):
        response = self.modern(
            "tools/call", {"name": "do_my_taxes", "arguments": {}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.content)["error"]["code"], p.INVALID_PARAMS
        )

    def test_throttling_stops_at_the_site_wide_limit(self):
        for _ in range(20):
            self.assertFalse(self.result(self.call(GOOD_ARGS))["isError"])
        result = self.result(self.call(GOOD_ARGS))
        self.assertTrue(result["isError"])
        self.assertIn("Rate limit", result["content"][0]["text"])
        self.assertEqual(ContactMessage.objects.count(), 20)

    def test_listing_tools_does_not_consume_quota(self):
        for _ in range(50):
            self.modern("tools/list")
        self.assertFalse(self.result(self.call(GOOD_ARGS))["isError"])

    def test_the_record_is_append_only_from_the_admin(self):
        from inbox.admin import ContactMessageAdmin
        self.assertFalse(ContactMessageAdmin.has_add_permission(None, None))
        self.assertFalse(ContactMessageAdmin.has_delete_permission(None, None))


class TransportTests(McpTestCase):
    def test_get_is_405_but_explains_itself(self):
        response = self.client.get("/mcp")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST, OPTIONS")
        payload = json.loads(response.content)
        self.assertEqual(payload["transport"], "streamable-http")
        self.assertEqual(payload["tools"], ["request_human_assistance"])

    def test_trailing_slash_reaches_the_same_endpoint(self):
        response = self.client.post(
            "/mcp/",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            content_type="application/json",
        )
        self.assertEqual(self.result(response), {})

    def test_malformed_json_is_a_parse_error(self):
        response = self.client.post(
            "/mcp", data="{not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["error"]["code"], p.PARSE_ERROR)

    def test_batches_are_refused(self):
        response = self.post([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            json.loads(response.content)["error"]["code"], p.INVALID_REQUEST
        )

    def test_session_and_resumption_headers_are_ignored(self):
        response = self.modern(
            "ping", headers={"Mcp-Session-Id": "abc", "Last-Event-ID": "7"}
        )
        self.assertEqual(self.result(response), {})
        self.assertNotIn("Mcp-Session-Id", response)

    def test_a_web_origin_gets_cors_headers_without_credentials(self):
        response = self.modern("ping", headers={"Origin": "https://client.example"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Access-Control-Allow-Origin"], "https://client.example"
        )
        self.assertNotIn("Access-Control-Allow-Credentials", response)

    def test_a_non_web_origin_is_forbidden(self):
        response = self.modern("ping", headers={"Origin": "null"})
        self.assertEqual(response.status_code, 403)

    def test_preflight(self):
        response = self.client.options(
            "/mcp", headers={"Origin": "https://client.example"}
        )
        self.assertEqual(response.status_code, 204)
        self.assertIn("MCP-Protocol-Version", response["Access-Control-Allow-Headers"])


class RegistryAuthTests(TestCase):
    def test_absent_until_configured(self):
        self.assertEqual(self.client.get("/.well-known/mcp-registry-auth").status_code, 404)

    def test_serves_the_proof_line_as_plain_text(self):
        proof = "v=MCPv1; k=ed25519; p=AAAA"
        with self.settings(MCP_REGISTRY_AUTH=proof):
            response = self.client.get("/.well-known/mcp-registry-auth")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(response.content.decode().strip(), proof)
