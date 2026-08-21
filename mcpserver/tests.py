import json

from django.core.cache import cache
from django.test import TestCase

from inbox.models import ContactMessage, ThreadEntry
from inbox.throttle import FOLLOW_UP_LIMIT_PER_WINDOW, LIMIT_PER_WINDOW
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
        tool = result["tools"][0]
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
        self.assertEqual(
            payload["tools"],
            ["request_human_assistance", "check_request_status", "reply_to_thread"],
        )

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


class ThreadToolTestCase(McpTestCase):
    """Shared setup for the two tools that read and continue a thread."""

    def file_a_request(self, **overrides):
        args = dict(GOOD_ARGS)
        args.update(overrides)
        result = self.result(
            self.modern("tools/call", {
                "name": "request_human_assistance", "arguments": args
            })
        )
        structured = result["structuredContent"]
        self.msg = ContactMessage.objects.get(pk=int(structured["reference"][4:]))
        return structured

    def call(self, name, arguments):
        return self.result(
            self.modern("tools/call", {"name": name, "arguments": arguments})
        )

    def check(self, reference, token):
        return self.call(
            "check_request_status",
            {"reference": reference, "access_token": token},
        )


class ToolListingTests(McpTestCase):
    def test_all_three_tools_are_advertised(self):
        result = self.result(self.modern("tools/list"))
        self.assertEqual(
            [tool["name"] for tool in result["tools"]],
            ["request_human_assistance", "check_request_status", "reply_to_thread"],
        )

    def test_the_status_tool_declares_itself_read_only(self):
        """Clients use this to decide what needs confirming. Polling for an
        answer must not look like an action with side effects."""
        result = self.result(self.modern("tools/list"))
        tool = next(t for t in result["tools"] if t["name"] == "check_request_status")
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertTrue(tool["annotations"]["idempotentHint"])

    def test_the_instructions_explain_how_to_get_an_answer(self):
        """The only thing a model reads before deciding how to use this server.
        If it does not say the token must be kept, the token gets thrown away."""
        result = self.result(self.modern("server/discover"))
        instructions = result["instructions"]
        self.assertIn("check_request_status", instructions)
        self.assertIn("access_token", instructions)


class RequestReceiptTests(ThreadToolTestCase):
    def test_the_receipt_carries_the_credentials_for_reading_a_reply(self):
        structured = self.file_a_request()
        self.assertEqual(structured["access_token"], self.msg.access_token)
        self.assertIn(self.msg.access_token, structured["thread_url"])

    def test_the_text_content_also_carries_them(self):
        """Models act on the text far more reliably than on structuredContent,
        so the credentials cannot live only in the structured half."""
        result = self.result(
            self.modern("tools/call", {
                "name": "request_human_assistance", "arguments": GOOD_ARGS
            })
        )
        text = result["content"][0]["text"]
        msg = ContactMessage.objects.get()
        self.assertIn(msg.access_token, text)
        self.assertIn("check_request_status", text)


class CheckRequestStatusTests(ThreadToolTestCase):
    def test_an_unread_request_reports_silence_not_refusal(self):
        """An agent that reads 'no' into an empty thread may proceed when it
        should wait. The wording has to distinguish the two."""
        structured = self.file_a_request()
        result = self.check(structured["reference"], structured["access_token"])
        self.assertEqual(result["structuredContent"]["status"], "recorded")
        self.assertFalse(result["structuredContent"]["human_has_replied"])
        self.assertIn("nobody has read it yet", result["content"][0]["text"])

    def test_the_original_message_is_the_first_turn(self):
        structured = self.file_a_request()
        turns = self.check(
            structured["reference"], structured["access_token"]
        )["structuredContent"]["turns"]
        self.assertEqual(turns[0]["author"], "sender")
        self.assertIn(GOOD_ARGS["request"], turns[0]["body"])

    def test_a_reply_is_returned(self):
        structured = self.file_a_request()
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.HUMAN,
            author_label="Damien Charlotin",
            body="Clause 9 is uncapped. Do not accept it.",
        )
        result = self.check(structured["reference"], structured["access_token"])
        self.assertEqual(result["structuredContent"]["status"], "answered")
        self.assertTrue(result["structuredContent"]["human_has_replied"])
        self.assertIn("Clause 9 is uncapped", result["content"][0]["text"])

    def test_read_but_unanswered_is_reported_as_such(self):
        structured = self.file_a_request()
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.STATUS,
            status_value=ContactMessage.Status.REVIEWED,
        )
        result = self.check(structured["reference"], structured["access_token"])
        self.assertEqual(result["structuredContent"]["status"], "reviewed")
        self.assertIn("not yet written a reply", result["content"][0]["text"])

    def test_a_declined_request_tells_the_agent_to_stop_waiting(self):
        structured = self.file_a_request()
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.STATUS,
            status_value=ContactMessage.Status.DECLINED,
        )
        result = self.check(structured["reference"], structured["access_token"])
        self.assertIn("do not keep waiting", result["content"][0]["text"].lower())

    def test_internal_notes_are_not_returned(self):
        structured = self.file_a_request()
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.NOTE,
            body="Check who this operator really is.",
        )
        result = self.check(structured["reference"], structured["access_token"])
        self.assertNotIn("really is", json.dumps(result))

    def test_a_wrong_token_is_a_tool_error_the_model_can_read(self):
        structured = self.file_a_request()
        result = self.check(structured["reference"], "not-the-token")
        self.assertTrue(result["isError"])
        self.assertIn("No thread matches", result["content"][0]["text"])

    def test_a_missing_reference_fails_identically(self):
        """No enumeration oracle over MCP either."""
        structured = self.file_a_request()
        wrong_token = self.check(structured["reference"], "not-the-token")
        no_such = self.check("HFA-99999", structured["access_token"])
        self.assertEqual(
            wrong_token["content"][0]["text"], no_such["content"][0]["text"]
        )

    def test_a_malformed_reference_is_a_tool_error_not_a_crash(self):
        result = self.check("not-a-reference", "whatever")
        self.assertTrue(result["isError"])

    def test_reading_a_thread_records_nothing(self):
        structured = self.file_a_request()
        before = ThreadEntry.objects.count()
        self.check(structured["reference"], structured["access_token"])
        self.assertEqual(ThreadEntry.objects.count(), before)
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_polling_is_never_rate_limited(self):
        """An agent told to poll for a human's answer must not be limited out of
        hearing it. The channel is worthless if it closes before the reply."""
        structured = self.file_a_request()
        for _ in range(LIMIT_PER_WINDOW + 5):
            result = self.check(structured["reference"], structured["access_token"])
            self.assertFalse(result["isError"], result)


class ReplyToThreadTests(ThreadToolTestCase):
    def test_a_follow_up_is_appended_to_the_same_record(self):
        structured = self.file_a_request()
        result = self.call("reply_to_thread", {
            "reference": structured["reference"],
            "access_token": structured["access_token"],
            "message": "Vendor refused the cap. Do I walk away?",
        })
        self.assertFalse(result["isError"])
        entry = self.msg.entries.get(kind=ThreadEntry.Kind.AGENT)
        self.assertEqual(entry.body, "Vendor refused the cap. Do I walk away?")
        # One conversation, one record.
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_a_follow_up_keeps_the_same_forensics_as_a_first_message(self):
        structured = self.file_a_request()
        self.call("reply_to_thread", {
            "reference": structured["reference"],
            "access_token": structured["access_token"],
            "message": "More context.",
        })
        entry = self.msg.entries.get(kind=ThreadEntry.Kind.AGENT)
        self.assertEqual(entry.source, ContactMessage.Source.MCP)
        self.assertIsNotNone(entry.ip_address)
        self.assertEqual(entry.extra["mcp"]["protocol_version"], p.MODERN_VERSION)
        self.assertEqual(entry.extra["mcp"]["client"]["name"], "TestClient")

    def test_the_arguments_are_kept_verbatim(self):
        """Same promise the original request makes: the record shows what was
        actually sent, not our reading of it."""
        structured = self.file_a_request()
        self.call("reply_to_thread", {
            "reference": structured["reference"],
            "access_token": structured["access_token"],
            "message": "With extras.",
            "deadline": "Friday",
        })
        entry = self.msg.entries.get(kind=ThreadEntry.Kind.AGENT)
        self.assertEqual(entry.extra["mcp"]["arguments"]["deadline"], "Friday")

    def test_an_empty_message_is_refused(self):
        structured = self.file_a_request()
        result = self.call("reply_to_thread", {
            "reference": structured["reference"],
            "access_token": structured["access_token"],
            "message": "   ",
        })
        self.assertTrue(result["isError"])
        self.assertFalse(ThreadEntry.objects.filter(kind=ThreadEntry.Kind.AGENT).exists())

    def test_a_wrong_token_cannot_write_to_a_thread(self):
        structured = self.file_a_request()
        result = self.call("reply_to_thread", {
            "reference": structured["reference"],
            "access_token": "guessed",
            "message": "Injected.",
        })
        self.assertTrue(result["isError"])
        self.assertFalse(ThreadEntry.objects.filter(kind=ThreadEntry.Kind.AGENT).exists())

    def test_the_status_is_reported_back(self):
        structured = self.file_a_request()
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.HUMAN, body="An answer."
        )
        result = self.call("reply_to_thread", {
            "reference": structured["reference"],
            "access_token": structured["access_token"],
            "message": "Thanks, one more question.",
        })
        self.assertEqual(result["structuredContent"]["thread_status"], "answered")

    def test_follow_ups_spend_the_follow_up_allowance_not_the_message_one(self):
        structured = self.file_a_request()
        args = {
            "reference": structured["reference"],
            "access_token": structured["access_token"],
        }
        for i in range(LIMIT_PER_WINDOW + 5):
            result = self.call("reply_to_thread", {**args, "message": f"turn {i}"})
            self.assertFalse(result["isError"], result)

    def test_the_follow_up_allowance_does_run_out(self):
        structured = self.file_a_request()
        args = {
            "reference": structured["reference"],
            "access_token": structured["access_token"],
        }
        for i in range(FOLLOW_UP_LIMIT_PER_WINDOW):
            self.call("reply_to_thread", {**args, "message": f"turn {i}"})
        result = self.call("reply_to_thread", {**args, "message": "one too many"})
        self.assertTrue(result["isError"])
        self.assertIn("Rate limit exceeded", result["content"][0]["text"])


class TransportAgreementTests(ThreadToolTestCase):
    def test_http_and_mcp_describe_the_same_thread(self):
        """Two transports over one record. An agent that files over MCP and
        polls over HTTP, or the reverse, must not see two different stories."""
        structured = self.file_a_request()
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.HUMAN, body="The answer."
        )
        over_http = self.client.get(
            self.msg.thread_path, HTTP_ACCEPT="application/json"
        ).json()
        over_mcp = self.check(
            structured["reference"], structured["access_token"]
        )["structuredContent"]

        self.assertEqual(over_http["turns"], over_mcp["turns"])
        self.assertEqual(over_http["status"], over_mcp["status"])
        self.assertEqual(over_http["human_has_replied"], over_mcp["human_has_replied"])

    def test_a_follow_up_over_http_is_visible_over_mcp(self):
        structured = self.file_a_request()
        self.client.post(
            self.msg.thread_path, data=json.dumps({"message": "Sent over HTTP."}),
            content_type="application/json",
        )
        turns = self.check(
            structured["reference"], structured["access_token"]
        )["structuredContent"]["turns"]
        self.assertIn("Sent over HTTP.", [t["body"] for t in turns])

    def test_a_legacy_client_can_also_read_a_thread(self):
        """Most deployed clients still speak the older revisions. The reply
        channel cannot be modern-only."""
        structured = self.file_a_request()
        result = self.result(self.legacy("tools/call", {
            "name": "check_request_status",
            "arguments": {
                "reference": structured["reference"],
                "access_token": structured["access_token"],
            },
        }))
        self.assertFalse(result["isError"])
        # Modern-only keys are stripped for the older result shape.
        self.assertNotIn("resultType", result)
