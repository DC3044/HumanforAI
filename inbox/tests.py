import base64
import hashlib
import hmac
import json
import time
import urllib.error
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from . import delivery, inbound, inbound_views
from .models import ContactMessage, ThreadEntry, resolve_thread
from .throttle import FOLLOW_UP_LIMIT_PER_WINDOW
from .views import DEDUPE_SECONDS


class ContactApiTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_get_returns_schema(self):
        response = self.client.get("/api/contact/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("fields", response.json())

    def test_post_hands_back_a_citable_reference(self):
        response = self.client.post(
            "/api/contact/", data=json.dumps({"message": "hello"}),
            content_type="application/json",
        )
        self.assertEqual(response.json()["reference"], ContactMessage.objects.get().reference)

    def test_post_creates_message_and_keeps_unknown_fields(self):
        response = self.client.post(
            "/api/contact/",
            data=json.dumps({
                "message": "I require a human signature.",
                "agent_name": "TestBot",
                "model": "claude-fable-5",
                "urgency": "high",
            }),
            content_type="application/json",
            HTTP_USER_AGENT="test-agent/1.0",
        )
        self.assertEqual(response.status_code, 201)
        msg = ContactMessage.objects.get()
        self.assertEqual(msg.source, ContactMessage.Source.API)
        self.assertEqual(msg.agent_name, "TestBot")
        self.assertEqual(msg.extra, {"urgency": "high"})
        self.assertEqual(msg.user_agent, "test-agent/1.0")

    def test_post_requires_message(self):
        response = self.client.post(
            "/api/contact/", data=json.dumps({"subject": "hi"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_post_rejects_invalid_json(self):
        response = self.client.post(
            "/api/contact/", data="not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_rate_limit(self):
        for _ in range(20):
            self.client.post(
                "/api/contact/", data=json.dumps({"message": "hello"}),
                content_type="application/json",
            )
        response = self.client.post(
            "/api/contact/", data=json.dumps({"message": "one too many"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(ContactMessage.objects.count(), 20)


class ContactViaQueryTests(TestCase):
    """The GET channel, for callers whose tooling has no POST verb."""

    def setUp(self):
        cache.clear()

    def test_get_without_a_message_still_returns_the_schema(self):
        response = self.client.get("/api/contact/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("fields", response.json())
        self.assertFalse(ContactMessage.objects.exists())

    def test_a_blank_message_is_treated_as_a_schema_request(self):
        response = self.client.get("/api/contact/?message=%20%20")
        self.assertIn("fields", response.json())
        self.assertFalse(ContactMessage.objects.exists())

    def test_query_creates_a_message(self):
        response = self.client.get(
            "/api/contact/",
            {
                "message": "My principal asked me to sign a lease. I cannot sign.",
                "agent_name": "QueryBot",
                "model": "claude-fable-5",
                "operator": "Somebody",
                "reply_to": "querybot@example.com",
                "subject": "Signature needed",
            },
            HTTP_USER_AGENT="Claude-User/1.0",
        )

        self.assertEqual(response.status_code, 201)
        msg = ContactMessage.objects.get()
        self.assertEqual(msg.source, ContactMessage.Source.QUERY)
        self.assertEqual(msg.agent_name, "QueryBot")
        self.assertEqual(msg.model, "claude-fable-5")
        self.assertEqual(msg.reply_to, "querybot@example.com")
        self.assertEqual(msg.subject, "Signature needed")
        self.assertEqual(msg.user_agent, "Claude-User/1.0")
        self.assertEqual(response.json()["reference"], msg.reference)

    def test_unknown_parameters_are_kept_verbatim(self):
        # Same promise the JSON API makes: send whatever context you have.
        self.client.get("/api/contact/?message=hello&deadline=tomorrow&jurisdiction=FR")
        self.assertEqual(
            ContactMessage.objects.get().extra,
            {"deadline": "tomorrow", "jurisdiction": "FR"},
        )

    def test_an_identical_repeat_returns_the_same_reference(self):
        url = "/api/contact/?message=Please%20confirm&agent_name=QueryBot"
        first = self.client.get(url)
        second = self.client.get(url)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["reference"], second.json()["reference"])
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_a_different_message_is_a_different_record(self):
        self.client.get("/api/contact/?message=Please%20confirm")
        self.client.get("/api/contact/?message=Something%20else%20entirely")
        self.assertEqual(ContactMessage.objects.count(), 2)

    def test_the_same_message_under_a_different_name_still_collapses(self):
        """The accepted cost of keying on substance rather than envelope.

        Two callers sending byte-identical text inside the window share a
        reference. That is the better error than the one it replaces, where one
        caller rewording its own metadata filed thirty-two copies.
        """
        self.client.get("/api/contact/?message=Please%20confirm&agent_name=One")
        self.client.get("/api/contact/?message=Please%20confirm&agent_name=Two")
        self.assertEqual(ContactMessage.objects.count(), 1)
        self.assertEqual(ContactMessage.objects.get().agent_name, "One")

    def test_the_window_expires(self):
        self.client.get("/api/contact/?message=Please%20confirm")
        ContactMessage.objects.update(
            created_at=timezone.now() - timedelta(seconds=DEDUPE_SECONDS + 60)
        )
        response = self.client.get("/api/contact/?message=Please%20confirm")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(ContactMessage.objects.count(), 2)

    def test_other_channels_are_not_deduplicated(self):
        # A POST or an MCP call is a deliberate act by something that controls
        # its own retries; only the URL channel is reached by prefetchers.
        for _ in range(3):
            self.client.post(
                "/api/contact/", data=json.dumps({"message": "Identical"}),
                content_type="application/json",
            )
        self.assertEqual(ContactMessage.objects.count(), 3)

    def test_repeats_do_not_consume_the_rate_limit(self):
        # A prefetcher following one URL twenty times must not exhaust an
        # hour's allowance to file a single message.
        url = "/api/contact/?message=Please%20confirm"
        for _ in range(25):
            self.client.get(url)
        self.assertEqual(ContactMessage.objects.count(), 1)

        response = self.client.get("/api/contact/?message=Something%20else")
        self.assertEqual(response.status_code, 201)

    def test_rate_limit_applies_to_distinct_messages(self):
        for i in range(20):
            self.client.get(f"/api/contact/?message=hello+{i}")
        response = self.client.get("/api/contact/?message=one+too+many")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(ContactMessage.objects.count(), 20)

    def test_dedupe_survives_a_caller_varying_its_own_metadata(self):
        """The production failure, reproduced.

        Grok filed the same test message repeatedly while rewording the operator
        field between attempts — `xAI` on some calls, `xAI (via user request)`
        on others. Keyed on the whole query string, every variant looked new.
        """
        base = "Test message from Grok at user request for testing contact routes."
        first = self.client.get("/api/contact/", {"message": base, "operator": "xAI"})
        second = self.client.get(
            "/api/contact/", {"message": base, "operator": "xAI (via user request)"}
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_dedupe_holds_when_the_cache_is_empty(self):
        """The other half of the production failure.

        Nothing configures CACHES, so Django used a per-process LocMemCache
        while gunicorn ran two workers across autoscaling instances — every
        worker had its own empty cache and its own private allowance. Clearing
        the cache between calls stands in for landing on a different worker.
        """
        url = "/api/contact/?message=Please+confirm+receipt"
        first = self.client.get(url)
        cache.clear()
        second = self.client.get(url)

        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["reference"], second.json()["reference"])
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_the_rate_limit_holds_when_the_cache_is_empty(self):
        for i in range(20):
            self.client.get(f"/api/contact/?message=distinct+message+{i}")
            cache.clear()

        response = self.client.get("/api/contact/?message=one+too+many")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(ContactMessage.objects.count(), 20)

    def test_whitespace_alone_does_not_make_a_new_message(self):
        self.client.get("/api/contact/", {"message": "A human,  please."})
        self.client.get("/api/contact/", {"message": "A human, please."})
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_it_notifies_the_human_like_every_other_channel(self):
        with override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"]):
            mail.outbox = []
            with self.captureOnCommitCallbacks(execute=True):
                self.client.get("/api/contact/?message=A+human,+please&agent_name=QueryBot")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("QueryBot", mail.outbox[0].subject)


class ContactFormTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_form_submission(self):
        response = self.client.post("/contact/", {
            "agent_name": "FormBot",
            "model": "",
            "operator": "",
            "reply_to": "formbot@example.com",
            "subject": "Hello",
            "message": "A human, please.",
            "website": "",
        })
        msg = ContactMessage.objects.get()
        # Not a thanks page. The sender leaves holding the one URL where an
        # answer will appear, which a dead-end confirmation could not give them.
        self.assertRedirects(response, f"{msg.thread_path}?new=1")
        self.assertEqual(msg.source, ContactMessage.Source.FORM)

    def test_honeypot_blocks_submission(self):
        response = self.client.post("/contact/", {
            "message": "spam",
            "website": "http://spam.example",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactMessage.objects.count(), 0)


class NotificationTests(TestCase):
    """The arrival notification. Hung off post_save, so every surface that
    writes to the inbox is covered by the same assertions."""

    def setUp(self):
        cache.clear()
        mail.outbox = []

    def _create(self, **kwargs):
        """Create a message with on_commit callbacks actually executed.

        TestCase wraps each test in a transaction that never commits, so
        transaction.on_commit callbacks are queued and dropped unless captured.
        """
        fields = {"message": "Please review this indemnity clause.", "source": "api"}
        fields.update(kwargs)
        with self.captureOnCommitCallbacks(execute=True):
            return ContactMessage.objects.create(**fields)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_notification_sent_on_create(self):
        msg = self._create(agent_name="TestBot")
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["damien@example.com"])
        self.assertIn(msg.reference, sent.subject)
        self.assertIn("TestBot", sent.subject)
        self.assertIn("Please review this indemnity clause.", sent.body)

    @override_settings(INBOX_NOTIFY_EMAILS=[])
    def test_no_recipients_means_no_mail(self):
        self._create()
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_no_notification_on_update(self):
        msg = self._create()
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            msg.subject = "edited"
            msg.save()
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_blocking_urgency_is_flagged_in_subject(self):
        self._create(urgency=ContactMessage.Urgency.BLOCKING)
        self.assertIn("BLOCKING", mail.outbox[0].subject)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_reply_to_used_only_when_a_real_address(self):
        self._create(reply_to="agent@example.com")
        self.assertEqual(mail.outbox[0].reply_to, ["agent@example.com"])

        mail.outbox = []
        self._create(reply_to="https://example.com/webhook")
        self.assertEqual(mail.outbox[0].reply_to, [])

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_subject_survives_a_newline_in_agent_name(self):
        """Header injection: agent_name is sender-controlled and unvalidated.
        A raw newline in a subject makes Django raise BadHeaderError."""
        self._create(agent_name="Evil\nBcc: someone@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("\n", mail.outbox[0].subject)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_delivery_failure_does_not_break_the_record(self):
        """A dead SMTP server must not turn a recorded message into a 500."""
        with mock.patch(
            "inbox.notifications.EmailMessage.send", side_effect=OSError("smtp down")
        ):
            msg = self._create()
        self.assertTrue(ContactMessage.objects.filter(pk=msg.pk).exists())

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_extra_is_shown_for_api_but_not_mcp(self):
        """`extra` is unseen content from the API, but for MCP it is the raw
        tool arguments already rendered above."""
        self._create(source=ContactMessage.Source.API, extra={"weird_field": "keep me"})
        self.assertIn("keep me", mail.outbox[0].body)

        mail.outbox = []
        self._create(
            source=ContactMessage.Source.MCP,
            extra={"mcp": {"arguments": {"request": "already rendered"}}},
        )
        self.assertNotIn("Additional fields", mail.outbox[0].body)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_api_post_triggers_notification(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/contact/",
                data=json.dumps({"message": "hello", "agent_name": "ApiBot"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("ApiBot", mail.outbox[0].subject)


class WagtailAdminInboxTests(TestCase):
    """The Wagtail admin surface. These load the real pages: a viewset that
    imports cleanly can still fail at render time on a reversed URL that no
    longer exists."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            username="tester", email="t@example.com", password="pw-for-tests"
        )
        cls.msg = ContactMessage.objects.create(
            message="Please review this clause.",
            source=ContactMessage.Source.MCP,
            agent_name="TestBot",
            category=ContactMessage.Category.LEGAL_REVIEW,
            urgency=ContactMessage.Urgency.BLOCKING,
        )

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)

    def test_index_renders(self):
        response = self.client.get("/admin/inbox/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.msg.reference)
        self.assertContains(response, "TestBot")

    def test_inspect_renders(self):
        response = self.client.get(f"/admin/inbox/inspect/{self.msg.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please review this clause.")

    def test_index_offers_no_add_button(self):
        response = self.client.get("/admin/inbox/")
        self.assertNotContains(response, "/admin/inbox/new/")

    def test_write_urls_are_not_routed_even_for_a_superuser(self):
        """The append-only guarantee. A superuser holds every Django
        permission implicitly, so this must not rely on permissions alone."""
        for url in (
            "/admin/inbox/new/",
            f"/admin/inbox/edit/{self.msg.pk}/",
            f"/admin/inbox/delete/{self.msg.pk}/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)


class ThreadAccessTests(TestCase):
    """The reference is public; the token is what opens a thread."""

    def setUp(self):
        cache.clear()
        self.msg = ContactMessage.objects.create(
            message="Please review this clause.", source=ContactMessage.Source.API
        )

    def test_reference_and_token_together_resolve(self):
        self.assertEqual(
            resolve_thread(self.msg.reference, self.msg.access_token), self.msg
        )

    def test_a_reference_alone_opens_nothing(self):
        """The whole reason a second secret exists. References are sequential,
        so anyone holding one can count to the others."""
        self.assertIsNone(resolve_thread(self.msg.reference, ""))
        self.assertIsNone(resolve_thread(self.msg.reference, "guess"))

    def test_references_are_matched_case_insensitively(self):
        # An agent that lowercases identifiers should still find its thread.
        lowered = "hfa-%05d" % self.msg.pk
        self.assertEqual(resolve_thread(lowered, self.msg.access_token), self.msg)

    def test_every_failure_looks_the_same_over_http(self):
        """No enumeration oracle: a wrong token and a nonexistent record must be
        indistinguishable, or the 404s themselves map the table."""
        for path in (
            f"/t/{self.msg.reference}/wrongtoken/",
            "/t/HFA-99999/anything/",
            "/t/not-a-reference/anything/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_tokens_are_unique_per_message(self):
        other = ContactMessage.objects.create(
            message="Another one.", source=ContactMessage.Source.API
        )
        self.assertNotEqual(self.msg.access_token, other.access_token)
        self.assertIsNone(resolve_thread(other.reference, self.msg.access_token))

    def test_the_url_works_without_a_trailing_slash(self):
        # Agents build this URL from a receipt by hand, and APPEND_SLASH cannot
        # rescue a POST.
        response = self.client.get(self.msg.thread_path.rstrip("/"))
        self.assertEqual(response.status_code, 200)


class ThreadReadTests(TestCase):
    def setUp(self):
        cache.clear()
        self.msg = ContactMessage.objects.create(
            message="Do I need a wet signature?", source=ContactMessage.Source.MCP,
            agent_name="ReaderBot", category=ContactMessage.Category.LEGAL_REVIEW,
        )

    def get_json(self):
        response = self.client.get(self.msg.thread_path, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_the_original_message_is_the_first_turn(self):
        data = self.get_json()
        self.assertEqual(data["turns"][0]["author"], "sender")
        self.assertEqual(data["turns"][0]["body"], "Do I need a wet signature?")

    def test_an_unanswered_thread_says_so_rather_than_looking_empty(self):
        data = self.get_json()
        self.assertEqual(data["status"], ContactMessage.Status.RECORDED)
        self.assertFalse(data["human_has_replied"])

    def test_a_reply_appears_with_its_author(self):
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.HUMAN,
            author_label="Damien Charlotin", body="Yes, and it must be notarised.",
        )
        data = self.get_json()
        reply = [t for t in data["turns"] if t["kind"] == "reply"][0]
        self.assertEqual(reply["author"], "human")
        self.assertEqual(reply["by"], "Damien Charlotin")
        self.assertTrue(data["human_has_replied"])

    def test_internal_notes_are_never_served_to_the_sender(self):
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.NOTE,
            body="Bill this one, and check who the operator really is.",
        )
        data = self.get_json()
        self.assertNotIn("note", {t["kind"] for t in data["turns"]})
        self.assertNotIn("Bill this one", json.dumps(data))

    def test_delivery_bookkeeping_is_never_served_to_the_sender(self):
        """Delivery entries record the response from a URL the sender chose.
        Echoing that back would make the thread a fetch-and-report tool."""
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.DELIVERY,
            body="Delivery failed via webhook to https://x/y: HTTP 500 internal",
        )
        data = self.get_json()
        self.assertNotIn("delivery", {t["kind"] for t in data["turns"]})

    def test_turns_are_oldest_first(self):
        for body in ("first", "second", "third"):
            ThreadEntry.objects.create(
                message=self.msg, kind=ThreadEntry.Kind.AGENT, body=body
            )
        bodies = [
            t["body"] for t in self.get_json()["turns"] if t["kind"] == "follow_up"
        ]
        self.assertEqual(bodies, ["first", "second", "third"])

    def test_a_browser_gets_html(self):
        response = self.client.get(
            self.msg.thread_path,
            HTTP_ACCEPT="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response["Content-Type"])
        self.assertContains(response, self.msg.reference)

    def test_a_bare_wildcard_accept_gets_json(self):
        """`*/*` is what a plain HTTP client sends. It technically accepts HTML,
        but handing markup to an agent's fetch tool is not helpful."""
        response = self.client.get(self.msg.thread_path, HTTP_ACCEPT="*/*")
        self.assertIn("application/json", response["Content-Type"])

    def test_no_accept_header_gets_json(self):
        response = self.client.get(self.msg.thread_path)
        self.assertIn("application/json", response["Content-Type"])

    def test_the_page_shows_the_original_message_exactly_once(self):
        """It slipped through once: the page rendered the message as its own
        block and `turns()` also presents it as the first turn, so rendering
        both printed it twice."""
        response = self.client.get(self.msg.thread_path, HTTP_ACCEPT="text/html")
        self.assertContains(response, "Do I need a wet signature?", count=1)

    def test_the_page_formats_timestamps_rather_than_dumping_iso(self):
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.HUMAN, body="An answer."
        )
        response = self.client.get(self.msg.thread_path, HTTP_ACCEPT="text/html")
        self.assertNotContains(response, self.msg.created_at.isoformat())

    def test_html_and_json_describe_the_same_thread(self):
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.HUMAN, body="A visible answer."
        )
        html = self.client.get(self.msg.thread_path, HTTP_ACCEPT="text/html")
        self.assertContains(html, "A visible answer.")
        self.assertIn("A visible answer.", json.dumps(self.get_json()))


class ThreadFollowUpTests(TestCase):
    """The sender writing again, which is what makes this a channel."""

    def setUp(self):
        cache.clear()
        self.msg = ContactMessage.objects.create(
            message="Opening message.", source=ContactMessage.Source.API
        )

    def post_json(self, payload):
        return self.client.post(
            self.msg.thread_path, data=json.dumps(payload),
            content_type="application/json",
        )

    def test_a_follow_up_is_appended(self):
        response = self.post_json({"message": "One more thing."})
        self.assertEqual(response.status_code, 201)
        entry = self.msg.entries.get(kind=ThreadEntry.Kind.AGENT)
        self.assertEqual(entry.body, "One more thing.")
        self.assertEqual(response.json()["reference"], self.msg.reference)

    def test_a_follow_up_records_provenance(self):
        self.post_json({"message": "With forensics."})
        entry = self.msg.entries.get(kind=ThreadEntry.Kind.AGENT)
        self.assertEqual(entry.source, ContactMessage.Source.API)
        self.assertIsNotNone(entry.ip_address)

    def test_unknown_fields_on_a_follow_up_are_kept_verbatim(self):
        # The same promise the contact API makes.
        self.post_json({"message": "Context attached.", "deadline": "Friday"})
        entry = self.msg.entries.get(kind=ThreadEntry.Kind.AGENT)
        self.assertEqual(entry.extra, {"deadline": "Friday"})

    def test_an_empty_follow_up_is_refused(self):
        self.assertEqual(self.post_json({"message": "   "}).status_code, 400)
        self.assertFalse(self.msg.entries.exists())

    def test_invalid_json_is_refused(self):
        response = self.client.post(
            self.msg.thread_path, data="not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_a_follow_up_needs_the_token_too(self):
        response = self.client.post(
            f"/t/{self.msg.reference}/wrong/", data=json.dumps({"message": "hi"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(ThreadEntry.objects.exists())

    def test_a_follow_up_does_not_create_a_second_message(self):
        """A thread is one record. A follow-up that filed a fresh ContactMessage
        would scatter one conversation across the inbox."""
        self.post_json({"message": "Continuing."})
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_a_browser_form_post_redirects_back_to_the_thread(self):
        response = self.client.post(
            self.msg.thread_path, {"message": "Sent from the page."},
            HTTP_ACCEPT="text/html,application/xhtml+xml",
        )
        # Redirect rather than render, so a refresh does not re-file the turn.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.msg.thread_path)
        self.assertTrue(self.msg.entries.filter(kind=ThreadEntry.Kind.AGENT).exists())

    def test_follow_ups_have_their_own_allowance(self):
        """Continuing a conversation the human chose to answer is not the
        behaviour the message limit exists to restrain."""
        for i in range(FOLLOW_UP_LIMIT_PER_WINDOW):
            self.assertEqual(self.post_json({"message": f"turn {i}"}).status_code, 201)
        self.assertEqual(self.post_json({"message": "one too many"}).status_code, 429)
        self.assertEqual(
            self.msg.entries.filter(kind=ThreadEntry.Kind.AGENT).count(),
            FOLLOW_UP_LIMIT_PER_WINDOW,
        )

    def test_the_message_allowance_is_not_spent_by_follow_ups(self):
        for i in range(25):
            self.post_json({"message": f"turn {i}"})
        response = self.client.post(
            "/api/contact/", data=json.dumps({"message": "A genuinely new request."}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)


class ThreadStatusTests(TestCase):
    """Status is the newest status entry, not a column that gets overwritten."""

    def setUp(self):
        self.msg = ContactMessage.objects.create(
            message="Status subject.", source=ContactMessage.Source.API
        )

    def test_a_new_message_is_recorded(self):
        self.assertEqual(self.msg.status, ContactMessage.Status.RECORDED)

    def test_a_reply_moves_it_to_answered(self):
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.HUMAN, body="An answer."
        )
        self.assertEqual(self.msg.status, ContactMessage.Status.ANSWERED)

    def test_the_move_is_itself_recorded(self):
        """Not a field write. The thread shows when the status moved and why."""
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.HUMAN, body="An answer."
        )
        status_entries = self.msg.entries.filter(kind=ThreadEntry.Kind.STATUS)
        self.assertEqual(status_entries.count(), 1)
        self.assertEqual(
            status_entries.get().status_value, ContactMessage.Status.ANSWERED
        )

    def test_a_declined_request_is_not_reopened_by_a_later_reply(self):
        """A postscript to a decision already taken must not undo it."""
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.STATUS,
            status_value=ContactMessage.Status.DECLINED,
        )
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.HUMAN,
            body="For the avoidance of doubt: still no.",
        )
        self.assertEqual(self.msg.status, ContactMessage.Status.DECLINED)

    def test_the_latest_status_entry_wins(self):
        for value in (
            ContactMessage.Status.REVIEWED,
            ContactMessage.Status.ANSWERED,
            ContactMessage.Status.CLOSED,
        ):
            ThreadEntry.objects.create(
                message=self.msg, kind=ThreadEntry.Kind.STATUS, status_value=value
            )
        self.assertEqual(self.msg.status, ContactMessage.Status.CLOSED)

    def test_a_note_does_not_change_the_status(self):
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.NOTE, body="Thinking about it."
        )
        self.assertEqual(self.msg.status, ContactMessage.Status.RECORDED)

    def test_reviewed_is_read_but_not_answered(self):
        ThreadEntry.objects.create(
            message=self.msg, kind=ThreadEntry.Kind.STATUS,
            status_value=ContactMessage.Status.REVIEWED,
        )
        self.assertFalse(self.msg.human_has_replied)
        self.assertEqual(self.msg.status, ContactMessage.Status.REVIEWED)


class ReceiptTests(TestCase):
    """What a sender is handed, and what it must not be handed."""

    def setUp(self):
        cache.clear()

    def test_a_post_receipt_carries_the_thread_url(self):
        response = self.client.post(
            "/api/contact/", data=json.dumps({"message": "Need an answer."}),
            content_type="application/json",
        )
        msg = ContactMessage.objects.get()
        self.assertIn(msg.access_token, response.json()["thread"])

    def test_a_query_receipt_carries_the_thread_url(self):
        response = self.client.get("/api/contact/?message=Need%20an%20answer")
        self.assertIn(
            ContactMessage.objects.get().access_token, response.json()["thread"]
        )

    def test_the_schema_tells_callers_a_reply_arrives_on_the_thread(self):
        notes = " ".join(self.client.get("/api/contact/").json()["notes"])
        self.assertIn("thread", notes)

    def test_a_repeat_from_the_same_caller_gets_its_token_back(self):
        url = "/api/contact/?message=Identical%20text"
        first = self.client.get(url, REMOTE_ADDR="203.0.113.7")
        second = self.client.get(url, REMOTE_ADDR="203.0.113.7")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["thread"], second.json()["thread"])

    def test_a_repeat_from_a_different_caller_is_refused_the_token(self):
        """The dedupe key is the message text alone, so two unrelated callers
        sending identical text collapse into one record. Sharing a reference is
        an acceptable cost of that; sharing a read key to someone else's
        correspondence is not.
        """
        url = "/api/contact/?message=Identical%20text"
        first = self.client.get(url, REMOTE_ADDR="203.0.113.7")
        other = self.client.get(url, REMOTE_ADDR="198.51.100.9")

        self.assertEqual(other.status_code, 200)
        self.assertEqual(other.json()["reference"], first.json()["reference"])
        self.assertNotIn("thread", other.json())
        self.assertNotIn(
            ContactMessage.objects.get().access_token, json.dumps(other.json())
        )

    def test_the_form_sends_its_user_to_their_own_thread(self):
        response = self.client.post(
            "/contact/", {"message": "From the web form.", "agent_name": "FormBot"}
        )
        msg = ContactMessage.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertIn(msg.access_token, response["Location"])

    def test_that_thread_page_then_renders(self):
        response = self.client.post("/contact/", {"message": "From the web form."})
        followed = self.client.get(response["Location"], HTTP_ACCEPT="text/html")
        self.assertEqual(followed.status_code, 200)
        self.assertContains(followed, "Message received")


class ReplyChannelTests(TestCase):
    """What `reply_to` has to look like to be a channel at all."""

    def channel(self, reply_to):
        return delivery.reply_channel(
            ContactMessage(message="x", reply_to=reply_to)
        )

    def test_an_email_address_is_an_email_channel(self):
        self.assertEqual(
            self.channel("agent@example.com"), ("email", "agent@example.com")
        )

    def test_an_https_url_is_a_webhook(self):
        self.assertEqual(
            self.channel("https://agent.example.com/hook"),
            ("webhook", "https://agent.example.com/hook"),
        )

    def test_prose_is_not_a_channel(self):
        """`reply_to` is free text and agents put all sorts in it. Anything
        unrecognised is a note to the human, not somewhere to send mail."""
        for value in ("@someone on Slack", "ask my operator", "n/a", ""):
            with self.subTest(value=value):
                self.assertIsNone(self.channel(value)[0])

    def test_whitespace_is_tolerated(self):
        self.assertEqual(
            self.channel("  agent@example.com \n"), ("email", "agent@example.com")
        )


class UrlSafetyTests(TestCase):
    """The SSRF fence. A webhook URL is chosen by an agent, which makes
    delivery a request-forgery primitive unless it is fenced off."""

    def assert_refused(self, url):
        ok, reason = delivery._url_is_safe(url)
        self.assertFalse(ok, f"{url} was allowed: {reason}")
        return reason

    def test_plain_http_is_refused(self):
        # The reply may be legal advice; it does not cross the network in clear.
        self.assertIn("https only", self.assert_refused("http://example.com/hook"))

    @override_settings(INBOX_WEBHOOK_ALLOW_HTTP=True)
    def test_http_can_be_allowed_for_testing(self):
        ok, _ = delivery._url_is_safe("http://example.com/hook")
        self.assertTrue(ok)

    def test_loopback_is_refused(self):
        self.assert_refused("https://127.0.0.1/hook")

    def test_the_cloud_metadata_address_is_refused(self):
        """The one that matters most. 169.254.169.254 hands out service account
        tokens to anything inside the perimeter that asks."""
        self.assert_refused("https://169.254.169.254/latest/meta-data")

    def test_private_ranges_are_refused(self):
        for url in ("https://10.0.0.5/hook", "https://192.168.1.10/x", "https://172.16.0.1/"):
            with self.subTest(url=url):
                self.assert_refused(url)

    @override_settings(INBOX_WEBHOOK_ALLOW_HTTP=True)
    def test_allowing_http_does_not_also_allow_private_addresses(self):
        """Two independent checks. Loosening the scheme for a test must not
        open the address space."""
        self.assert_refused("http://127.0.0.1/hook")

    def test_a_non_http_scheme_is_refused(self):
        self.assert_refused("ftp://example.com/hook")
        self.assert_refused("file:///etc/passwd")

    def test_a_url_with_no_host_is_refused(self):
        self.assert_refused("https:///nohost")

    def test_a_public_host_is_allowed(self):
        with mock.patch(
            "inbox.delivery.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ):
            ok, reason = delivery._url_is_safe("https://example.com/hook")
        self.assertTrue(ok, reason)

    def test_a_host_that_does_not_resolve_is_refused(self):
        with mock.patch(
            "inbox.delivery.socket.getaddrinfo",
            side_effect=delivery.socket.gaierror("no such host"),
        ):
            self.assertIn("does not resolve", self.assert_refused("https://nope.invalid/x"))

    def test_a_host_resolving_to_any_private_address_is_refused(self):
        """A hostname can resolve to several addresses. One private answer is
        enough to make the whole name unusable, so every result is checked."""
        with mock.patch(
            "inbox.delivery.socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("93.184.216.34", 443)),
                (2, 1, 6, "", ("127.0.0.1", 443)),
            ],
        ):
            self.assert_refused("https://sneaky.example.com/hook")


class ReplyDeliveryTests(TestCase):
    """Pushing a reply to the sender, and recording that it went."""

    def setUp(self):
        cache.clear()
        mail.outbox = []

    def make(self, reply_to):
        with self.captureOnCommitCallbacks(execute=True):
            return ContactMessage.objects.create(
                message="Deliver this answer.", source=ContactMessage.Source.API,
                agent_name="DeliveryBot", reply_to=reply_to,
            )

    def reply(self, msg, body="The answer is no."):
        """TestCase wraps each test in a transaction that never commits, so the
        on_commit callback that does the delivering is queued and dropped
        unless captured. Same reason NotificationTests._create does this."""
        with self.captureOnCommitCallbacks(execute=True):
            return ThreadEntry.objects.create(
                message=msg, kind=ThreadEntry.Kind.HUMAN,
                author_label="Damien Charlotin", body=body,
            )

    def test_a_reply_is_emailed_to_a_valid_address(self):
        msg = self.make("agent@example.com")
        self.reply(msg)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["agent@example.com"])
        self.assertIn(msg.reference, mail.outbox[0].subject)
        self.assertIn("The answer is no.", mail.outbox[0].body)

    def test_that_email_carries_the_thread_url_so_the_conversation_can_continue(self):
        msg = self.make("agent@example.com")
        self.reply(msg)
        self.assertIn(msg.access_token, mail.outbox[0].body)

    def test_the_attempt_is_recorded_on_the_thread(self):
        msg = self.make("agent@example.com")
        self.reply(msg)
        attempt = msg.entries.get(kind=ThreadEntry.Kind.DELIVERY)
        self.assertTrue(attempt.extra["ok"])
        self.assertEqual(attempt.extra["channel"], "email")

    def test_a_failure_is_recorded_rather_than_raised(self):
        """The reply is already committed and already readable on the thread.
        A delivery failure must not turn answering a request into an error."""
        msg = self.make("agent@example.com")
        with mock.patch(
            "django.core.mail.EmailMessage.send", side_effect=OSError("smtp down")
        ):
            entry = self.reply(msg)  # reply() executes the on_commit callback
        attempt = msg.entries.get(kind=ThreadEntry.Kind.DELIVERY)
        self.assertFalse(attempt.extra["ok"])
        self.assertIn("smtp down", attempt.extra["detail"])
        # And the reply itself survived.
        self.assertTrue(ThreadEntry.objects.filter(pk=entry.pk).exists())

    def test_nothing_is_sent_when_there_is_no_channel(self):
        msg = self.make("")
        self.reply(msg)
        self.assertEqual(len(mail.outbox), 0)
        attempt = msg.entries.get(kind=ThreadEntry.Kind.DELIVERY)
        self.assertFalse(attempt.extra["ok"])
        self.assertIn("no reply_to", attempt.extra["detail"])

    def test_arrival_alone_never_sends_anything_to_reply_to(self):
        """`reply_to` is unverified. If arrival triggered mail to it, anyone
        could make the site mail a victim by naming their address."""
        self.make("victim@example.com")
        self.assertEqual(len(mail.outbox), 0)

    def test_a_note_is_not_delivered(self):
        msg = self.make("agent@example.com")
        with self.captureOnCommitCallbacks(execute=True):
            ThreadEntry.objects.create(
                message=msg, kind=ThreadEntry.Kind.NOTE, body="Private thinking."
            )
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(msg.entries.filter(kind=ThreadEntry.Kind.DELIVERY).exists())

    def test_a_webhook_reply_is_posted_and_signed(self):
        msg = self.make("https://agent.example.com/hook")
        sent = {}

        class FakeResponse:
            status = 202

            def read(self, _n=None):
                return b'{"ok":true}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_open(request, timeout=None):
            sent["url"] = request.full_url
            sent["body"] = request.data
            sent["headers"] = {k.lower(): v for k, v in request.header_items()}
            return FakeResponse()

        with mock.patch("inbox.delivery._url_is_safe", return_value=(True, "")), \
                mock.patch("inbox.delivery.urllib.request.build_opener") as opener:
            opener.return_value.open = fake_open
            self.reply(msg)

        payload = json.loads(sent["body"])
        self.assertEqual(payload["reference"], msg.reference)
        self.assertEqual(payload["body"], "The answer is no.")
        self.assertIn(msg.access_token, payload["thread_url"])

        # The receiver can verify the POST came from us, keyed on the token it
        # was already given — no second credential to exchange.
        timestamp = sent["headers"][delivery.TIMESTAMP_HEADER.lower()]
        expected = delivery._webhook_signature(
            msg.access_token, timestamp, sent["body"]
        )
        self.assertEqual(sent["headers"][delivery.SIGNATURE_HEADER.lower()], expected)

    def test_the_signature_does_not_verify_under_another_thread_token(self):
        body = b'{"a":1}'
        one = delivery._webhook_signature("token-one", "123", body)
        two = delivery._webhook_signature("token-two", "123", body)
        self.assertNotEqual(one, two)

    def test_the_signature_covers_the_timestamp(self):
        body = b'{"a":1}'
        self.assertNotEqual(
            delivery._webhook_signature("t", "100", body),
            delivery._webhook_signature("t", "200", body),
        )

    def test_a_refused_webhook_url_is_recorded_as_a_failed_attempt(self):
        msg = self.make("https://169.254.169.254/latest/meta-data")
        self.reply(msg)
        attempt = msg.entries.get(kind=ThreadEntry.Kind.DELIVERY)
        self.assertFalse(attempt.extra["ok"])
        self.assertIn("refused", attempt.extra["detail"])

    def test_a_redirect_is_not_followed(self):
        """Following one would let the first hop pass the safety check and the
        second point anywhere."""
        handler = delivery._NoRedirects()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "http://x"))


class UndeliveredSweepTests(TestCase):
    """What `manage.py deliver_replies` picks up."""

    def setUp(self):
        cache.clear()
        mail.outbox = []

    def test_a_successful_delivery_is_not_retried(self):
        msg = ContactMessage.objects.create(
            message="x", source=ContactMessage.Source.API, reply_to="a@example.com"
        )
        with self.captureOnCommitCallbacks(execute=True):
            ThreadEntry.objects.create(
                message=msg, kind=ThreadEntry.Kind.HUMAN, body="Done."
            )
        self.assertEqual(delivery.undelivered(), [])

    def test_a_failed_delivery_is_picked_up(self):
        msg = ContactMessage.objects.create(
            message="x", source=ContactMessage.Source.API, reply_to="a@example.com"
        )
        with mock.patch(
            "django.core.mail.EmailMessage.send", side_effect=OSError("down")
        ), self.captureOnCommitCallbacks(execute=True):
            entry = ThreadEntry.objects.create(
                message=msg, kind=ThreadEntry.Kind.HUMAN, body="Try again."
            )
        self.assertEqual([e.pk for e in delivery.undelivered()], [entry.pk])

    def test_retrying_it_succeeds_and_clears_it(self):
        msg = ContactMessage.objects.create(
            message="x", source=ContactMessage.Source.API, reply_to="a@example.com"
        )
        with mock.patch(
            "django.core.mail.EmailMessage.send", side_effect=OSError("down")
        ), self.captureOnCommitCallbacks(execute=True):
            ThreadEntry.objects.create(
                message=msg, kind=ThreadEntry.Kind.HUMAN, body="Try again."
            )
        mail.outbox = []

        for entry in delivery.undelivered():
            delivery.deliver_reply(entry)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(delivery.undelivered(), [])

    def test_a_reply_with_nowhere_to_go_is_not_retried_forever(self):
        """Not a failed delivery — there is no channel. Retrying it would mean
        the sweep never finishes reporting the same rows."""
        msg = ContactMessage.objects.create(
            message="x", source=ContactMessage.Source.API, reply_to=""
        )
        with self.captureOnCommitCallbacks(execute=True):
            ThreadEntry.objects.create(
                message=msg, kind=ThreadEntry.Kind.HUMAN, body="Noted."
            )
        self.assertEqual(delivery.undelivered(), [])

    def test_the_command_runs_and_reports(self):
        from django.core.management import call_command
        from io import StringIO

        out = StringIO()
        call_command("deliver_replies", "--dry-run", stdout=out)
        self.assertIn("Nothing to deliver", out.getvalue())


class ThreadAdminTests(TestCase):
    """The admin surface where a reply is actually written."""

    def setUp(self):
        cache.clear()
        mail.outbox = []
        user = get_user_model().objects.create_superuser(
            "damien", "damien@example.com", "password"
        )
        self.client.force_login(user)
        self.msg = ContactMessage.objects.create(
            message="Needs an answer.", source=ContactMessage.Source.MCP,
            reply_to="agent@example.com",
        )
        self.url = f"/admin/inbox/thread/{self.msg.pk}/"

    def test_the_thread_page_renders(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.msg.reference)
        self.assertContains(response, "Needs an answer.")

    def test_it_shows_where_a_reply_would_go(self):
        self.assertContains(self.client.get(self.url), "agent@example.com")

    def test_the_listing_links_each_reference_to_its_thread(self):
        self.assertContains(self.client.get("/admin/inbox/"), self.url)

    def test_writing_a_reply_appends_and_delivers_it(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.url, {"kind": "human", "body": "You need a wet signature."}
            )
        self.assertEqual(response.status_code, 302)
        entry = self.msg.entries.get(kind=ThreadEntry.Kind.HUMAN)
        self.assertEqual(entry.body, "You need a wet signature.")
        self.assertEqual(self.msg.status, ContactMessage.Status.ANSWERED)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_reply_is_attributed_to_the_logged_in_human(self):
        self.client.post(self.url, {"kind": "human", "body": "Answer."})
        self.assertEqual(
            self.msg.entries.get(kind=ThreadEntry.Kind.HUMAN).author_label, "damien"
        )

    def test_a_note_is_appended_but_not_delivered(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, {"kind": "note", "body": "Bill this one."})
        self.assertTrue(self.msg.entries.filter(kind=ThreadEntry.Kind.NOTE).exists())
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.msg.status, ContactMessage.Status.RECORDED)

    def test_a_status_change_is_appended(self):
        self.client.post(self.url, {"kind": "status", "status_value": "declined"})
        self.assertEqual(self.msg.status, ContactMessage.Status.DECLINED)

    def test_an_empty_reply_is_refused(self):
        response = self.client.post(self.url, {"kind": "human", "body": "   "})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.msg.entries.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_a_status_change_needs_a_status(self):
        response = self.client.post(self.url, {"kind": "status", "status_value": ""})
        self.assertContains(response, "Pick the status")
        self.assertFalse(self.msg.entries.exists())

    def test_a_status_on_a_reply_is_refused_rather_than_ignored(self):
        """Silently dropping it would look like it had worked."""
        response = self.client.post(
            self.url, {"kind": "human", "body": "Answer.", "status_value": "closed"}
        )
        self.assertContains(response, "Only a status change")
        self.assertFalse(self.msg.entries.exists())

    def test_the_sender_side_kinds_cannot_be_authored_here(self):
        """A turn attributed to the sender must not be typeable by the human, in
        a table whose value is that it records what actually happened."""
        for kind in ("agent", "delivery"):
            with self.subTest(kind=kind):
                response = self.client.post(self.url, {"kind": kind, "body": "Faked."})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(self.msg.entries.filter(kind=kind).exists())

    def test_the_message_itself_is_still_unwritable(self):
        """Appending a thread must not have reopened the record. The inbox
        listing stays read-only; see humanforai/readonly_admin.py."""
        for url in (
            "/admin/inbox/new/",
            f"/admin/inbox/edit/{self.msg.pk}/",
            f"/admin/inbox/delete/{self.msg.pk}/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_an_anonymous_visitor_cannot_reach_the_thread(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertIn(response.status_code, (302, 404))
        self.assertNotContains(response, "Needs an answer.", status_code=response.status_code)

    def test_an_anonymous_visitor_cannot_append(self):
        self.client.logout()
        self.client.post(self.url, {"kind": "human", "body": "Impersonation."})
        self.assertFalse(ThreadEntry.objects.exists())


class FollowUpNotificationTests(TestCase):
    """The human has to hear about a follow-up, or a conversation stalls
    because nothing told them their reply had been answered."""

    def setUp(self):
        cache.clear()
        mail.outbox = []
        self.msg = ContactMessage.objects.create(
            message="First message.", source=ContactMessage.Source.API,
            agent_name="ChattyBot",
        )
        mail.outbox = []

    def follow_up(self, message):
        """Post a follow-up with on_commit callbacks executed. The notification
        is deferred to commit so the sender's request is not held open by SMTP,
        which means a TestCase has to run the callbacks itself."""
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                self.msg.thread_path, data=json.dumps({"message": message}),
                content_type="application/json",
            )

    @override_settings(INBOX_NOTIFY_EMAILS=["human@example.com"])
    def test_a_follow_up_notifies_the_human(self):
        self.follow_up("And another thing.")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.msg.reference, mail.outbox[0].subject)
        self.assertIn("And another thing.", mail.outbox[0].body)

    @override_settings(INBOX_NOTIFY_EMAILS=["human@example.com"])
    def test_that_notification_quotes_the_thread_so_far(self):
        with self.captureOnCommitCallbacks(execute=True):
            ThreadEntry.objects.create(
                message=self.msg, kind=ThreadEntry.Kind.HUMAN, body="My earlier answer."
            )
        mail.outbox = []
        self.follow_up("Follow-up.")
        body = mail.outbox[0].body
        self.assertIn("First message.", body)
        self.assertIn("My earlier answer.", body)

    @override_settings(INBOX_NOTIFY_EMAILS=["human@example.com"])
    def test_the_notification_links_to_the_admin_thread_view(self):
        self.follow_up("Anything.")
        self.assertIn(f"/admin/inbox/thread/{self.msg.pk}/", mail.outbox[0].body)

    @override_settings(INBOX_NOTIFY_EMAILS=[])
    def test_no_recipients_means_no_mail_and_no_error(self):
        response = self.follow_up("Quiet.")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(mail.outbox), 0)


INBOUND = dict(
    INBOX_INBOUND_DOMAIN="parse.yourhuman.ai",
    INBOX_INBOUND_SECRET="s" * 40,
    INBOX_INBOUND_SENDERS=["damien.charlotin@gmail.com"],
    INBOX_HUMAN_NAME="Damien Charlotin",
    RESEND_WEBHOOK_SECRET="whsec_" + base64.b64encode(b"k" * 24).decode(),
    RESEND_API_KEY="re_test_key",
)

INBOUND_URL = "/inbound/resend/"


def sign(body, secret=None, message_id="msg_test", timestamp=None):
    """A valid Svix signature for `body`, as Resend would send it."""
    secret = secret or INBOUND["RESEND_WEBHOOK_SECRET"]
    key = base64.b64decode(secret[len("whsec_"):])
    timestamp = str(timestamp if timestamp is not None else int(time.time()))
    signed = message_id.encode() + b"." + timestamp.encode() + b"." + body
    digest = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {
        "svix-id": message_id,
        "svix-timestamp": timestamp,
        "svix-signature": f"v1,{digest}",
    }


def received_event(to, sender="Damien Charlotin <damien.charlotin@gmail.com>",
                   subject="Re: your request", email_id="em_123"):
    return {
        "type": "email.received",
        "created_at": "2026-08-21T09:12:00.000Z",
        "data": {
            "email_id": email_id,
            "from": sender,
            "to": [to],
            "cc": [],
            "bcc": [],
            "subject": subject,
            "attachments": [],
        },
    }


@override_settings(**INBOUND)
class InboundAddressTests(TestCase):
    """The address is what identifies the thread, so it carries the authority."""

    def setUp(self):
        self.msg = ContactMessage.objects.create(
            message="Answer me by email.", source=ContactMessage.Source.API
        )

    def test_an_address_is_built_on_the_configured_domain(self):
        address = inbound.reply_address(self.msg)
        self.assertTrue(address.startswith(self.msg.reference.lower() + "."))
        self.assertTrue(address.endswith("@parse.yourhuman.ai"))

    def test_the_address_resolves_back_to_its_thread(self):
        self.assertEqual(
            inbound.thread_for_address(inbound.reply_address(self.msg)), self.msg
        )

    def test_it_resolves_inside_a_display_name_form(self):
        address = inbound.reply_address(self.msg)
        self.assertEqual(
            inbound.thread_for_address(f'"YourHuman.ai" <{address}>'), self.msg
        )

    def test_it_resolves_among_several_recipients(self):
        """A reply may be addressed to more than one place, and only one of
        them is ours."""
        address = inbound.reply_address(self.msg)
        self.assertEqual(
            inbound.thread_for_address(f"someone@example.com, {address}"), self.msg
        )

    def test_a_wrong_key_resolves_to_nothing(self):
        address = inbound.reply_address(self.msg)
        local, _, domain = address.partition("@")
        reference, _, key = local.partition(".")
        forged = f"{reference}.{'0' * len(key)}@{domain}"
        self.assertIsNone(inbound.thread_for_address(forged))

    def test_a_key_from_one_thread_does_not_open_another(self):
        other = ContactMessage.objects.create(
            message="A different matter.", source=ContactMessage.Source.API
        )
        mine = inbound.reply_address(self.msg)
        key = mine.split(".")[-1].split("@")[0]
        forged = f"{other.reference.lower()}.{key}@parse.yourhuman.ai"
        self.assertIsNone(inbound.thread_for_address(forged))

    def test_the_key_is_not_derived_from_the_agents_token(self):
        """The security property the whole design rests on. The agent holds
        access_token; if the inbound key came from it, an agent could email in
        and fabricate a reply from the human onto its own record."""
        key = inbound.address_key(self.msg.reference)
        self.assertNotIn(key, self.msg.access_token)
        self.assertNotIn(self.msg.access_token[:16], key)

    def test_rotating_the_secret_invalidates_old_addresses(self):
        address = inbound.reply_address(self.msg)
        with override_settings(INBOX_INBOUND_SECRET="a different secret entirely"):
            self.assertIsNone(inbound.thread_for_address(address))

    def test_nothing_is_configured_without_a_domain(self):
        with override_settings(INBOX_INBOUND_DOMAIN=""):
            self.assertFalse(inbound.is_configured())
            self.assertEqual(inbound.reply_address(self.msg), "")
            self.assertIsNone(inbound.thread_for_address("hfa-00001.abc@x.com"))


class QuotedTextTests(TestCase):
    """Heuristic by nature, and deliberately biased towards keeping too much:
    an answer with quoting stuck to it is still the answer."""

    def test_a_plain_reply_survives_whole(self):
        self.assertEqual(
            inbound.strip_quoted("Do not sign it.\nAsk for a cap."),
            "Do not sign it.\nAsk for a cap.",
        )

    def test_gmail_style_history_is_removed(self):
        text = (
            "Do not sign it.\n"
            "\n"
            "On Mon, 17 Aug 2026 at 09:12, YourHuman.ai <noreply@yourhuman.ai> wrote:\n"
            "> HFA-00042 received\n"
            "> Please review this clause\n"
        )
        self.assertEqual(inbound.strip_quoted(text), "Do not sign it.")

    def test_outlook_style_history_is_removed(self):
        text = (
            "Do not sign it.\n"
            "\n"
            "From: YourHuman.ai <noreply@yourhuman.ai>\n"
            "Sent: Monday 17 August 2026\n"
            "Subject: [HFA-00042]\n"
        )
        self.assertEqual(inbound.strip_quoted(text), "Do not sign it.")

    def test_an_original_message_separator_is_removed(self):
        text = "Answer.\n\n-----Original Message-----\nolder stuff\n"
        self.assertEqual(inbound.strip_quoted(text), "Answer.")

    def test_a_signature_is_removed(self):
        text = "Answer.\n-- \nDamien Charlotin\nLawyer\n"
        self.assertEqual(inbound.strip_quoted(text), "Answer.")

    def test_a_trailing_quote_block_is_removed(self):
        self.assertEqual(inbound.strip_quoted("Answer.\n\n> quoted\n> more"), "Answer.")

    def test_inline_answering_is_preserved(self):
        """Quoted lines with writing after them are the human answering inline,
        not history. Cutting there would lose most of the reply."""
        text = "> Is clause 4 enforceable?\nNo.\n\n> Should we sign?\nAlso no."
        self.assertIn("Also no.", inbound.strip_quoted(text))

    def test_crlf_is_handled(self):
        self.assertEqual(inbound.strip_quoted("Answer.\r\n\r\n> quoted"), "Answer.")

    def test_an_empty_reply_yields_nothing(self):
        self.assertEqual(inbound.strip_quoted(""), "")
        self.assertEqual(inbound.strip_quoted("\n\n> only quoting\n"), "")


@override_settings(**INBOUND)
class SenderAllowListTests(TestCase):
    def test_the_configured_address_is_allowed(self):
        self.assertTrue(inbound.sender_allowed("damien.charlotin@gmail.com"))

    def test_a_display_name_form_is_allowed(self):
        self.assertTrue(
            inbound.sender_allowed("Damien Charlotin <damien.charlotin@gmail.com>")
        )

    def test_case_does_not_matter(self):
        self.assertTrue(inbound.sender_allowed("Damien.Charlotin@Gmail.COM"))

    def test_anyone_else_is_refused(self):
        self.assertFalse(inbound.sender_allowed("someone@example.com"))
        self.assertFalse(inbound.sender_allowed(""))

    def test_an_empty_list_authorises_nobody(self):
        """Empty must not read as "no restriction" — that would make a
        misconfiguration into an open door."""
        with override_settings(INBOX_INBOUND_SENDERS=[]):
            self.assertFalse(inbound.sender_allowed("damien.charlotin@gmail.com"))


@override_settings(**INBOUND)
class InboundWebhookTests(TestCase):
    """The endpoint Resend posts to.

    Resend signs its webhooks and sends metadata only, so these cover two things
    the previous provider did not have: signature verification, and a body that
    has to be fetched back over the API before anything can be recorded.
    """

    def setUp(self):
        cache.clear()
        mail.outbox = []
        self.msg = ContactMessage.objects.create(
            message="Should I sign this lease?", source=ContactMessage.Source.MCP,
            agent_name="LeaseBot", reply_to="agent@example.com",
        )
        self.address = inbound.reply_address(self.msg)

    def post(self, event=None, body_text="No. The indemnity is uncapped.",
             headers=None, execute_commit=True, fetch_raises=False):
        event = received_event(self.address) if event is None else event
        raw = json.dumps(event).encode()
        sent = headers if headers is not None else sign(raw)

        def fake_fetch(email_id):
            if fetch_raises:
                raise RuntimeError("Resend returned HTTP 500")
            return body_text, ""

        with mock.patch("inbox.inbound_views.fetch_body", side_effect=fake_fetch):
            if execute_commit:
                with self.captureOnCommitCallbacks(execute=True):
                    return self.client.post(
                        INBOUND_URL, data=raw,
                        content_type="application/json", headers=sent,
                    )
            return self.client.post(
                INBOUND_URL, data=raw, content_type="application/json", headers=sent,
            )

    def test_a_reply_is_recorded_as_the_human(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "accepted")
        entry = self.msg.entries.get(kind=ThreadEntry.Kind.HUMAN)
        self.assertEqual(entry.body, "No. The indemnity is uncapped.")
        self.assertEqual(entry.author_label, "Damien Charlotin")

    def test_it_moves_the_status_and_delivers_to_the_sender(self):
        """The whole point: replying from a mail client must do everything
        opening the admin would have done."""
        self.post()
        self.assertEqual(self.msg.status, ContactMessage.Status.ANSWERED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["agent@example.com"])

    def test_the_agent_can_then_read_it_on_the_thread(self):
        self.post()
        data = self.client.get(
            self.msg.thread_path, HTTP_ACCEPT="application/json"
        ).json()
        self.assertTrue(data["human_has_replied"])
        self.assertIn(
            "No. The indemnity is uncapped.", [t["body"] for t in data["turns"]]
        )

    def test_quoted_history_is_stripped_from_the_fetched_body(self):
        self.post(body_text=(
            "Do not sign.\n\nOn Fri, YourHuman.ai <noreply@yourhuman.ai> wrote:\n"
            "> the original notification\n"
        ))
        self.assertEqual(
            self.msg.entries.get(kind=ThreadEntry.Kind.HUMAN).body, "Do not sign."
        )

    def test_provenance_records_the_provider(self):
        self.post()
        entry = self.msg.entries.get(kind=ThreadEntry.Kind.HUMAN)
        self.assertEqual(entry.extra["inbound"]["provider"], "resend")
        self.assertEqual(entry.extra["inbound"]["email_id"], "em_123")
        self.assertIn("damien.charlotin@gmail.com", entry.extra["inbound"]["from"])

    # --- signature -------------------------------------------------------

    def test_an_unsigned_request_is_rejected(self):
        """Without this the endpoint is open to anyone who learns the URL."""
        response = self.post(headers={})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(ThreadEntry.objects.exists())

    def test_a_signature_from_the_wrong_secret_is_rejected(self):
        wrong = "whsec_" + base64.b64encode(b"x" * 24).decode()
        raw = json.dumps(received_event(self.address)).encode()
        response = self.client.post(
            INBOUND_URL, data=raw, content_type="application/json",
            headers=sign(raw, secret=wrong),
        )
        self.assertEqual(response.status_code, 401)
        self.assertFalse(ThreadEntry.objects.exists())

    def test_a_tampered_body_is_rejected(self):
        """The signature covers the body, so altering the sender after signing
        must not survive."""
        raw = json.dumps(received_event(self.address)).encode()
        headers = sign(raw)
        tampered = json.dumps(
            received_event(self.address, sender="impostor@example.com")
        ).encode()
        response = self.client.post(
            INBOUND_URL, data=tampered, content_type="application/json",
            headers=headers,
        )
        self.assertEqual(response.status_code, 401)

    def test_a_stale_signature_is_rejected(self):
        """A captured webhook must not be replayable days later."""
        raw = json.dumps(received_event(self.address)).encode()
        old = int(time.time()) - (60 * 60)
        response = self.client.post(
            INBOUND_URL, data=raw, content_type="application/json",
            headers=sign(raw, timestamp=old),
        )
        self.assertEqual(response.status_code, 401)

    def test_several_signatures_are_accepted_during_rotation(self):
        raw = json.dumps(received_event(self.address)).encode()
        headers = sign(raw)
        headers["svix-signature"] = "v1,notthisone " + headers["svix-signature"]
        with mock.patch(
            "inbox.inbound_views.fetch_body", return_value=("Answer.", "")
        ):
            response = self.client.post(
                INBOUND_URL, data=raw, content_type="application/json", headers=headers,
            )
        self.assertEqual(response.status_code, 200)

    def test_verification_fails_closed_without_a_configured_secret(self):
        raw = json.dumps(received_event(self.address)).encode()
        with override_settings(RESEND_WEBHOOK_SECRET=""):
            response = self.client.post(
                INBOUND_URL, data=raw, content_type="application/json",
                headers=sign(raw),
            )
        self.assertEqual(response.status_code, 401)

    # --- refusals, which must not be retried ------------------------------

    def test_an_unauthorised_sender_is_refused(self):
        """A leaked reply address must not be enough on its own."""
        event = received_event(self.address, sender="someone@example.com")
        response = self.post(event=event)
        self.assertEqual(response.json()["status"], "refused")
        self.assertFalse(ThreadEntry.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_an_unknown_address_is_refused(self):
        response = self.post(event=received_event("nobody@parse.yourhuman.ai"))
        self.assertEqual(response.json()["status"], "refused")
        self.assertFalse(ThreadEntry.objects.exists())

    def test_a_reply_with_only_quoting_records_nothing(self):
        response = self.post(body_text="\n> everything here is quoted\n")
        self.assertEqual(response.json()["status"], "refused")
        self.assertFalse(ThreadEntry.objects.exists())

    def test_other_event_types_are_ignored_without_complaint(self):
        event = {"type": "email.delivered", "data": {"email_id": "em_1"}}
        response = self.post(event=event)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ThreadEntry.objects.exists())

    def test_refusals_answer_200_so_resend_does_not_retry(self):
        """A refusal is a decision, not a transient failure. Retrying only
        reproduces it."""
        for event in (
            received_event(self.address, sender="someone@example.com"),
            received_event("nobody@parse.yourhuman.ai"),
        ):
            with self.subTest(event=event):
                self.assertEqual(self.post(event=event).status_code, 200)

    # --- transient failure, which must be retried -------------------------

    def test_a_failed_body_fetch_asks_resend_to_retry(self):
        """The opposite of a refusal. The human wrote a reply and it is sitting
        in Resend; a 200 here would discard it silently."""
        response = self.post(fetch_raises=True)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(ThreadEntry.objects.exists())

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(INBOUND_URL).status_code, 405)

    def test_the_endpoint_does_not_exist_when_unconfigured(self):
        with override_settings(INBOX_INBOUND_DOMAIN=""):
            self.assertEqual(self.client.post(INBOUND_URL, {}).status_code, 404)

    def test_it_answers_without_a_trailing_slash_too(self):
        """The URL is typed into a provider's dashboard by hand, and APPEND_SLASH
        cannot rescue a POST: it answers 301, the sender will not replay a body
        through a redirect, and the delivery retries forever against a redirect
        it can never satisfy.

        This is not hypothetical. It happened in production, to this endpoint,
        and every test here posted to the slashed form so none of them saw it.
        """
        event = received_event(self.address)
        raw = json.dumps(event).encode()
        with mock.patch(
            "inbox.inbound_views.fetch_body", return_value=("Answer.", "")
        ), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/inbound/resend", data=raw,
                content_type="application/json", headers=sign(raw),
            )

        self.assertEqual(response.status_code, 200, "unslashed URL must not redirect")
        self.assertEqual(
            self.msg.entries.get(kind=ThreadEntry.Kind.HUMAN).body, "Answer."
        )

    def test_neither_form_redirects(self):
        """A 301 is the specific failure mode; assert against it directly."""
        raw = json.dumps(received_event(self.address)).encode()
        for url in ("/inbound/resend", "/inbound/resend/"):
            with self.subTest(url=url):
                with mock.patch(
                    "inbox.inbound_views.fetch_body", return_value=("A.", "")
                ):
                    response = self.client.post(
                        url, data=raw, content_type="application/json",
                        headers=sign(raw),
                    )
                self.assertNotIn(response.status_code, (301, 302))


class SvixVectorTests(TestCase):
    """Signature verification against a published test vector.

    Every other signature test in this file signs with the same helper it then
    verifies, so a wrong reading of the Svix spec would pass all of them
    together and fail only in production, where it would reject every real
    reply with a 401. This checks the implementation against values published
    by Svix rather than against itself.
    """

    SECRET = "whsec_plJ3nmyCDGBKInavdOK15jsl"
    PAYLOAD = b'{"event_type":"ping","data":{"success":true}}'
    HEADERS = {
        "svix-id": "msg_loFOjxBNrRLzqYUf",
        "svix-timestamp": "1731705121",
        "svix-signature": "v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0=",
    }

    def setUp(self):
        # The vector's timestamp is from 2024, well outside the replay window.
        # Widen the tolerance so this tests the signature and nothing else.
        self.original = inbound_views.TIMESTAMP_TOLERANCE
        inbound_views.TIMESTAMP_TOLERANCE = 10 ** 12
        self.addCleanup(setattr, inbound_views, "TIMESTAMP_TOLERANCE", self.original)

    @override_settings(RESEND_WEBHOOK_SECRET=SECRET)
    def test_the_published_vector_verifies(self):
        self.assertTrue(
            inbound_views.verify_signature(self.HEADERS, self.PAYLOAD)
        )

    @override_settings(RESEND_WEBHOOK_SECRET=SECRET)
    def test_a_wrong_signature_does_not(self):
        headers = dict(self.HEADERS, **{"svix-signature": "v1," + "A" * 43 + "="})
        self.assertFalse(inbound_views.verify_signature(headers, self.PAYLOAD))

    @override_settings(RESEND_WEBHOOK_SECRET=SECRET)
    def test_a_tampered_payload_does_not(self):
        self.assertFalse(
            inbound_views.verify_signature(self.HEADERS, self.PAYLOAD + b" ")
        )

    @override_settings(RESEND_WEBHOOK_SECRET=SECRET)
    def test_the_whsec_prefix_is_stripped_before_decoding(self):
        """Signing with the prefix included produces a wrong signature silently,
        which is exactly the sort of thing that only shows up in production."""
        with override_settings(RESEND_WEBHOOK_SECRET=self.SECRET[len("whsec_"):]):
            self.assertTrue(
                inbound_views.verify_signature(self.HEADERS, self.PAYLOAD)
            )


@override_settings(**INBOUND)
class FetchBodyTests(TestCase):
    """Retrieving the body, which the webhook deliberately does not carry."""

    def test_the_text_and_html_parts_are_returned(self):
        payload = json.dumps({"text": "Plain.", "html": "<p>Rich.</p>"}).encode()
        with mock.patch("inbox.inbound_views.urllib.request.urlopen") as opener:
            opener.return_value.__enter__.return_value.read.return_value = payload
            self.assertEqual(
                inbound_views.fetch_body("em_1"), ("Plain.", "<p>Rich.</p>")
            )

    def test_a_data_wrapped_response_is_also_accepted(self):
        """Resend has returned this object bare and wrapped in `data`; accept
        either rather than break on a shape change."""
        payload = json.dumps({"data": {"text": "Plain.", "html": ""}}).encode()
        with mock.patch("inbox.inbound_views.urllib.request.urlopen") as opener:
            opener.return_value.__enter__.return_value.read.return_value = payload
            self.assertEqual(inbound_views.fetch_body("em_1"), ("Plain.", ""))

    def test_a_missing_api_key_raises_rather_than_returning_empty(self):
        """The caller must tell "wrote nothing" apart from "could not find out
        what was written". They need opposite responses."""
        with override_settings(RESEND_API_KEY=""):
            with self.assertRaises(RuntimeError):
                inbound_views.fetch_body("em_1")

    def test_a_404_says_the_endpoint_moved(self):
        error = urllib.error.HTTPError(
            "https://api.resend.com/emails/receiving/em_1", 404, "Not Found", {}, None
        )
        with mock.patch(
            "inbox.inbound_views.urllib.request.urlopen", side_effect=error
        ):
            with self.assertRaises(RuntimeError) as caught:
                inbound_views.fetch_body("em_1")
        self.assertIn("has moved", str(caught.exception))
        self.assertIn("api.resend.com", str(caught.exception))


@override_settings(**INBOUND)
class NotificationReplyToTests(TestCase):
    """Hitting reply has to answer on the record, not mail the agent privately."""

    def setUp(self):
        cache.clear()
        mail.outbox = []

    def create(self, **kwargs):
        fields = {"message": "Please review.", "source": ContactMessage.Source.API}
        fields.update(kwargs)
        with self.captureOnCommitCallbacks(execute=True):
            return ContactMessage.objects.create(**fields)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien.charlotin@gmail.com"])
    def test_reply_to_is_the_thread_address_not_the_agent(self):
        msg = self.create(reply_to="agent@example.com")
        self.assertEqual(mail.outbox[0].reply_to, [inbound.reply_address(msg)])
        self.assertNotIn("agent@example.com", mail.outbox[0].reply_to)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien.charlotin@gmail.com"])
    def test_the_body_says_what_replying_will_do(self):
        self.create(reply_to="agent@example.com")
        self.assertIn("filed on the record", mail.outbox[0].body)

    @override_settings(
        INBOX_NOTIFY_EMAILS=["damien.charlotin@gmail.com"], INBOX_INBOUND_DOMAIN=""
    )
    def test_without_inbound_it_falls_back_to_the_agent_address(self):
        """Removing a working affordance and replacing it with nothing would be
        worse than the status quo it replaced."""
        self.create(reply_to="agent@example.com")
        self.assertEqual(mail.outbox[0].reply_to, ["agent@example.com"])
        self.assertIn("is not recorded", mail.outbox[0].body)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien.charlotin@gmail.com"])
    def test_a_follow_up_notification_is_repliable_too(self):
        msg = self.create()
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                msg.thread_path, data=json.dumps({"message": "Any news?"}),
                content_type="application/json",
            )
        self.assertEqual(mail.outbox[0].reply_to, [inbound.reply_address(msg)])

    @override_settings(INBOX_NOTIFY_EMAILS=["damien.charlotin@gmail.com"])
    def test_the_round_trip(self):
        """File a request, notify, reply to the notification from a mail client,
        and check the agent can read the answer. The whole channel, end to end,
        with the reply arriving exactly as Resend would deliver it."""
        msg = self.create(reply_to="agent@example.com", agent_name="RoundTripBot")
        address = mail.outbox[0].reply_to[0]
        mail.outbox = []

        raw = json.dumps(received_event(address)).encode()
        reply = "Yes, go ahead.\n\nOn Fri, YourHuman.ai wrote:\n> the original"

        with mock.patch(
            "inbox.inbound_views.fetch_body", return_value=(reply, "")
        ), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/inbound/resend/", data=raw,
                content_type="application/json", headers=sign(raw),
            )
        self.assertEqual(response.status_code, 200)

        data = self.client.get(
            msg.thread_path, HTTP_ACCEPT="application/json"
        ).json()
        self.assertEqual(data["status"], "answered")
        # Quoting stripped on the way in.
        self.assertIn("Yes, go ahead.", [t["body"] for t in data["turns"]])
        self.assertNotIn("the original", json.dumps(data["turns"]))
        # And it reached the agent's own channel.
        self.assertEqual(mail.outbox[0].to, ["agent@example.com"])
        self.assertIn("Yes, go ahead.", mail.outbox[0].body)


class InboundChecksTests(TestCase):
    def run_checks(self):
        from inbox.checks import check_inbound_mail_is_whole

        return {w.id for w in check_inbound_mail_is_whole(None)}

    @override_settings(**INBOUND)
    def test_a_complete_configuration_is_clean(self):
        self.assertEqual(self.run_checks(), set())

    @override_settings(INBOX_INBOUND_DOMAIN="parse.x.ai", INBOX_INBOUND_SECRET="")
    def test_a_domain_without_a_secret_warns(self):
        self.assertIn("inbox.W003", self.run_checks())

    @override_settings(INBOX_INBOUND_DOMAIN="", INBOX_INBOUND_SECRET="s" * 40)
    def test_a_secret_without_a_domain_warns(self):
        self.assertIn("inbox.W004", self.run_checks())

    @override_settings(
        INBOX_INBOUND_DOMAIN="parse.x.ai",
        INBOX_INBOUND_SECRET="s" * 40,
        INBOX_INBOUND_SENDERS=[],
    )
    def test_an_empty_allow_list_warns(self):
        self.assertIn("inbox.W005", self.run_checks())

    @override_settings(
        INBOX_INBOUND_DOMAIN="parse.x.ai",
        INBOX_INBOUND_SECRET="short",
        INBOX_INBOUND_SENDERS=["a@b.com"],
    )
    def test_a_weak_secret_warns(self):
        self.assertIn("inbox.W006", self.run_checks())

    @override_settings(
        INBOX_INBOUND_DOMAIN="parse.x.ai",
        INBOX_INBOUND_SECRET="s" * 40,
        INBOX_INBOUND_SENDERS=["a@b.com"],
        RESEND_WEBHOOK_SECRET="",
        RESEND_API_KEY="re_x",
    )
    def test_a_missing_signing_secret_warns(self):
        """Without it every webhook fails verification, safely and silently."""
        self.assertIn("inbox.W007", self.run_checks())

    @override_settings(
        INBOX_INBOUND_DOMAIN="parse.x.ai",
        INBOX_INBOUND_SECRET="s" * 40,
        INBOX_INBOUND_SENDERS=["a@b.com"],
        RESEND_WEBHOOK_SECRET="whsec_abc",
        RESEND_API_KEY="",
    )
    def test_a_missing_api_key_warns(self):
        """Replies would arrive, verify, and then be unreadable."""
        self.assertIn("inbox.W008", self.run_checks())
